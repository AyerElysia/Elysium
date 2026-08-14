"""心跳 prompt 统一注入协议（SectionProvider）与主动性行动空间测试。

覆盖：
1. render_heartbeat_sections 循环：顺序、跳过、异常隔离
2. SendTargetsSection——心跳态看见"可以触达的人和地方"
3. speak 意向缺目标时的有声降级（note + available_targets）
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.send_targets import SendTarget
from plugins.life_engine.prompts.sections import (
    HeartbeatSectionProvider,
    SectionContext,
    SendTargetsSection,
    ThoughtStreamsSection,
    render_heartbeat_sections,
)
from plugins.life_engine.service import LifeEngineService
from plugins.life_engine.streams.manager import ThoughtStreamManager


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


def test_thought_streams_section_keeps_heading_format(tmp_path: Path) -> None:
    """等价性：思考流段落保持 '### 当前思考流\\n正文' 的原有结构。"""
    service = _make_service(tmp_path)
    service._thought_manager = ThoughtStreamManager(workspace_path=str(tmp_path))
    service._thought_manager.create(title="那段旋律")

    text = asyncio.run(ThoughtStreamsSection().render(_ctx(service)))

    assert text is not None
    assert text.startswith("### 当前思考流\n")
    assert "那段旋律" in text


# ── 2. 行动空间：可触达目标进心跳 ───────────────────────────


def _fake_targets() -> list[SendTarget]:
    return [
        SendTarget(
            target_key="p-abcd1234",
            stream_id="abcd1234ffff",
            platform="qq",
            chat_type="private",
            display_name="Ayer",
        ),
        SendTarget(
            target_key="g-efgh5678",
            stream_id="efgh5678ffff",
            platform="qq",
            chat_type="group",
            display_name="温柔的家",
        ),
    ]


def _patch_targets(monkeypatch: pytest.MonkeyPatch, targets: list[SendTarget]) -> None:
    async def fake_list(**kwargs):  # noqa: ANN003
        return targets

    monkeypatch.setattr(
        "plugins.life_engine.core.send_targets.list_recent_send_targets",
        fake_list,
    )


def test_send_targets_section_renders_map_without_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _make_service(tmp_path)
    _patch_targets(monkeypatch, _fake_targets())

    text = asyncio.run(SendTargetsSection().render(_ctx(service)))

    assert text is not None
    assert "### 你可以触达的人和地方" in text
    assert "target_key=p-abcd1234" in text
    assert "Ayer" in text
    assert "温柔的家" in text
    # 哲学约束：呈现地图，保留不行动的自由
    assert "没有想说的话也很好" in text


def test_send_targets_section_renders_fallback_when_no_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """近 24h 无活跃会话时，段落仍渲染——明确告知留空是正确选择，不静默消失。"""
    service = _make_service(tmp_path)
    _patch_targets(monkeypatch, [])

    text = asyncio.run(SendTargetsSection().render(_ctx(service)))

    assert text is not None
    assert "### 你可以触达的人和地方" in text
    assert "列表为空是正常状态" in text
    assert "不是记忆缺失" in text
    assert "留空" in text
    assert "由你重新判断" in text


def test_send_targets_section_respects_config_switch(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    service.plugin.config.autonomy.show_targets_in_heartbeat = False

    assert SendTargetsSection().enabled(_ctx(service)) is False


# ── 3. speak 意向缺目标时的有声降级 ─────────────────────────


def _prepare_autonomy(
    service: LifeEngineService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_register(plugin, intent) -> None:  # noqa: ANN001
        intent.schedule_id = "test_schedule"

    async def fake_queue(event) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(
        "plugins.life_engine.service.core.register_autonomy_schedule",
        fake_register,
    )
    monkeypatch.setattr(service, "_queue_pending_event", fake_queue)


def test_schedule_speak_without_target_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _make_service(tmp_path)
    _prepare_autonomy(service, monkeypatch)
    _patch_targets(monkeypatch, _fake_targets())

    result = asyncio.run(
        service.schedule_autonomy_intent(
            kind="speak", motivation="想他了", delay_minutes=5
        )
    )

    assert result["created"] is True
    assert "不会唤醒表达层" in result["note"]
    keys = [item["target_key"] for item in result["available_targets"]]
    assert "p-abcd1234" in keys


def test_schedule_speak_with_target_has_no_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _make_service(tmp_path)
    _prepare_autonomy(service, monkeypatch)

    result = asyncio.run(
        service.schedule_autonomy_intent(
            kind="speak",
            motivation="想他了",
            delay_minutes=5,
            target_stream_id="abcd1234ffff",
        )
    )

    assert result["created"] is True
    assert "note" not in result
    assert result["target_stream_id"] == "abcd1234ffff"
