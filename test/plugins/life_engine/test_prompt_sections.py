"""心跳 prompt 统一注入协议与 legacy autonomy 兼容测试。

覆盖：
1. render_heartbeat_sections 循环：顺序、跳过、异常隔离
2. legacy stream-bound 意向明确只读，且不再查询最近聊天流
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.prompts.sections import (
    HeartbeatSectionProvider,
    SectionContext,
    render_heartbeat_sections,
)
from plugins.life_engine.service import LifeEngineService


@dataclass
class _DummyPlugin:
    config: LifeEngineConfig


def _make_config(tmp_path: Path) -> LifeEngineConfig:
    cfg = LifeEngineConfig()
    cfg.settings.enabled = True
    cfg.settings.workspace_path = str(tmp_path)
    return cfg


def _make_service(tmp_path: Path) -> LifeEngineService:
    return LifeEngineService(_DummyPlugin(config=_make_config(tmp_path)))


def _ctx(service: LifeEngineService) -> SectionContext:
    return SectionContext(
        service=service,
        config=service.plugin.config,
        today_str="2026-06-11",
    )


# ── 1. 协议循环 ──────────────────────────────────────────────


class _StaticSection(HeartbeatSectionProvider):
    def __init__(self, section_id: str, text: str | None, *, on: bool = True) -> None:
        self.section_id = section_id
        self._text = text
        self._on = on

    def enabled(self, ctx: SectionContext) -> bool:
        return self._on

    async def render(self, ctx: SectionContext) -> str | None:
        return self._text


class _BrokenSection(HeartbeatSectionProvider):
    section_id = "broken"

    async def render(self, ctx: SectionContext) -> str | None:
        raise RuntimeError("boom")


def test_render_sections_preserves_order_and_skips_empty(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    providers = [
        _StaticSection("a", "段落A"),
        _StaticSection("b", None),
        _StaticSection("c", "   "),
        _StaticSection("d", "段落D"),
        _StaticSection("e", "段落E", on=False),
    ]

    texts = asyncio.run(render_heartbeat_sections(providers, _ctx(service)))

    assert texts == ["段落A", "段落D"]


def test_render_sections_isolates_provider_failure(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    providers = [
        _StaticSection("a", "段落A"),
        _BrokenSection(),
        _StaticSection("b", "段落B"),
    ]

    texts = asyncio.run(render_heartbeat_sections(providers, _ctx(service)))

    assert texts == ["段落A", "段落B"]


# ── 2. legacy stream-bound intent is read-only ─────────────


def test_legacy_schedule_is_rejected_without_target_lookup(
    tmp_path: Path,
) -> None:
    service = _make_service(tmp_path)
    with pytest.raises(RuntimeError, match="LegacyAutonomyReadOnly"):
        asyncio.run(
            service.schedule_autonomy_intent(
                kind="speak",
                motivation="想他了",
                delay_minutes=5,
                target_key="p-legacy",
            )
        )
