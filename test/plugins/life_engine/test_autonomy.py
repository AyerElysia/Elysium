"""life_engine autonomy intent tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.autonomy import AutonomyIntentStore, build_intent, schedule_autonomy_intent
from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.tools.autonomy_tools import LifeEngineScheduleAutonomyIntentTool
from src.core.models.message import Message


def _make_service(tmp_path: Path) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    plugin = SimpleNamespace(config=config)
    service = LifeEngineService(plugin)
    plugin.service = service
    return service


def test_build_intent_uses_delay_minutes_and_rejects_out_of_range() -> None:
    intent = build_intent(
        kind="reflect",
        motivation="我想等一会儿继续想这件事",
        delay_minutes=5,
        min_delay_minutes=1,
        max_delay_minutes=60,
    )

    assert intent.kind == "reflect"
    assert intent.delay_minutes == 5
    assert intent.status == "scheduled"
    assert intent.scheduled_at
    assert intent.task_name.startswith("life_autonomy::")

    with pytest.raises(ValueError, match="delay_minutes"):
        build_intent(
            kind="reflect",
            motivation="太久了",
            delay_minutes=120,
            min_delay_minutes=1,
            max_delay_minutes=60,
        )


@pytest.mark.asyncio
async def test_repeating_intent_uses_recurring_scheduler(monkeypatch) -> None:
    intent = build_intent(
        kind="reflect",
        motivation="每隔一小时醒来看看自己想做什么",
        delay_minutes=60,
        min_delay_minutes=1,
        max_delay_minutes=180,
        repeat=True,
    )
    captured: dict[str, Any] = {}

    class FakeScheduler:
        async def create_schedule(self, **kwargs):  # noqa: ANN001
            captured.update(kwargs)
            return "recurring-schedule"

    monkeypatch.setattr(
        "plugins.life_engine.autonomy.get_unified_scheduler",
        lambda: FakeScheduler(),
    )

    schedule_id = await schedule_autonomy_intent(SimpleNamespace(service=None), intent)

    assert schedule_id == "recurring-schedule"
    assert intent.schedule_id == "recurring-schedule"
    assert captured["is_recurring"] is True
    assert captured["trigger_config"]["interval_seconds"] == 3600.0


@pytest.mark.asyncio
async def test_schedule_autonomy_intent_persists_and_records_event(tmp_path: Path, monkeypatch) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-1"
        return "schedule-1"

    monkeypatch.setattr("plugins.life_engine.service.core.register_autonomy_schedule", fake_schedule)

    receipt = await service.schedule_autonomy_intent(
        kind="speak",
        motivation="我想过一会儿再确认要不要靠近",
        delay_minutes=3,
        target_hint="AyerElysia 私聊",
        target_stream_id="stream-1",
        constraints=["短一点", "不要催"],
    )

    assert receipt["created"] is True
    assert receipt["kind"] == "speak"
    assert receipt["delay_minutes"] == 3
    assert receipt["target_stream_id"] == "stream-1"

    stored = AutonomyIntentStore(tmp_path).get(receipt["intent_id"])
    assert stored is not None
    assert stored.schedule_id == "schedule-1"
    assert stored.constraints == ["短一点", "不要催"]

    assert len(service._pending_events) == 1
    assert service._pending_events[0].content_type == "autonomy_intent_scheduled"


@pytest.mark.asyncio
async def test_repeating_autonomy_intent_stays_scheduled_after_trigger(
    tmp_path: Path, monkeypatch
) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-1"
        return "schedule-1"

    monkeypatch.setattr("plugins.life_engine.service.core.register_autonomy_schedule", fake_schedule)
    receipt = await service.schedule_autonomy_intent(
        kind="reflect",
        motivation="每隔一小时醒来，自己判断要不要做点什么",
        delay_minutes=60,
        repeat=True,
    )
    service._pending_events.clear()

    result = await service.trigger_autonomy_intent(receipt["intent_id"])

    assert result["triggered"] is True
    assert result["dispatch"] == "life_engine"
    assert result["repeat"] is True
    assert result["occurrence_count"] == 1
    assert result["next_scheduled_at"]

    stored = AutonomyIntentStore(tmp_path).get(receipt["intent_id"])
    assert stored is not None
    assert stored.status == "scheduled"
    assert stored.repeat is True
    assert stored.interval_minutes == 60
    assert stored.occurrence_count == 1
    assert stored.triggered_at
    assert len(service._pending_events) == 1
    assert "周期性自主意向浮现" in service._pending_events[0].content


@pytest.mark.asyncio
async def test_autonomy_tool_calls_service(tmp_path: Path, monkeypatch) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-1"
        return "schedule-1"

    monkeypatch.setattr("plugins.life_engine.service.core.register_autonomy_schedule", fake_schedule)
    tool = LifeEngineScheduleAutonomyIntentTool(plugin=service.plugin)

    ok, result = await tool.execute(
        kind="silence",
        motivation="我想确认自己可以不打扰",
        delay_minutes=2,
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["kind"] == "silence"
    assert result["delay_minutes"] == 2


@pytest.mark.asyncio
async def test_trigger_speak_without_target_downgrades_to_life_event(tmp_path: Path, monkeypatch) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-1"
        return "schedule-1"

    monkeypatch.setattr("plugins.life_engine.service.core.register_autonomy_schedule", fake_schedule)
    receipt = await service.schedule_autonomy_intent(
        kind="speak",
        motivation="我想稍后再决定要不要说话",
        delay_minutes=1,
    )
    service._pending_events.clear()

    result = await service.trigger_autonomy_intent(receipt["intent_id"])

    assert result["triggered"] is True
    assert result["dispatch"] == "life_event"
    stored = AutonomyIntentStore(tmp_path).get(receipt["intent_id"])
    assert stored is not None
    assert stored.status == "triggered"
    assert len(service._pending_events) == 1
    assert service._pending_events[0].content_type == "autonomy_intent_due"


def test_life_chatter_treats_autonomy_trigger_as_internal_opportunity() -> None:
    message = Message(
        message_id="autonomy-1",
        content="自主意向浮现",
        processed_plain_text="自主意向浮现",
        is_autonomy_intent_trigger=True,
    )

    assert LifeChatter._is_proactive_trigger_message(message) is True
    assert LifeChatter._should_force_reply_for_unread_batch([message]) is False
