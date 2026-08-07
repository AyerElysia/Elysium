"""life_engine autonomy intent tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.autonomy import (
    AutonomyIntentStore,
    build_intent,
    restore_autonomy_intents,
    schedule_autonomy_intent,
)
from plugins.life_engine.core.chatter import LifeChatter, LifeSendTextAction
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.tools.autonomy_tools import LifeEngineScheduleAutonomyIntentTool
from src.core.config.core_config import CoreConfig
from src.core.models.message import Message
from src.core.models.stream import ChatStream


def _make_service(tmp_path: Path) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    plugin = SimpleNamespace(
        config=config,
        global_storage_config=CoreConfig(
            storage=CoreConfig.StorageSection(backend="local")
        ),
    )
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


async def test_repeating_intent_chains_one_shot_scheduler(monkeypatch) -> None:
    intent = build_intent(
        kind="reflect",
        motivation="每隔一小时醒来看看自己想做什么",
        delay_minutes=60,
        min_delay_minutes=1,
        max_delay_minutes=180,
        repeat=True,
        max_occurrences=4,
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
    assert captured["is_recurring"] is False
    assert "interval_seconds" not in captured["trigger_config"]


def test_repeating_intent_requires_an_explicit_execution_lease() -> None:
    with pytest.raises(ValueError, match="max_occurrences or lease_minutes"):
        build_intent(
            kind="speak",
            motivation="过一会儿再看看要不要开口",
            delay_minutes=2,
            repeat=True,
        )


async def test_restore_autonomy_requires_running_scheduler(
    tmp_path: Path, monkeypatch
) -> None:
    """Listing tasks must not be mistaken for scheduler readiness."""
    intent = build_intent(
        kind="reflect",
        motivation="稍后继续想",
        delay_minutes=5,
        min_delay_minutes=1,
        max_delay_minutes=60,
    )
    AutonomyIntentStore(tmp_path).upsert(intent)

    class StoppedScheduler:
        is_running = False

        async def create_schedule(self, **_kwargs):
            raise AssertionError("stopped scheduler must not receive schedules")

    monkeypatch.setattr(
        "plugins.life_engine.autonomy.get_unified_scheduler",
        lambda: StoppedScheduler(),
    )

    assert await restore_autonomy_intents(SimpleNamespace(service=None), tmp_path) == 0


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
        max_occurrences=3,
    )
    service._pending_events.clear()

    result = await service.trigger_autonomy_intent(receipt["intent_id"])

    assert result["triggered"] is True
    assert result["dispatch"] == "life_engine"
    assert result["repeat"] is True
    assert result["occurrence_count"] == 1

    stored = AutonomyIntentStore(tmp_path).get(receipt["intent_id"])
    assert stored is not None
    assert stored.status == "scheduled"
    assert stored.repeat is True
    assert stored.interval_minutes == 60
    assert stored.occurrence_count == 1
    assert stored.active_occurrence_id == ""
    assert stored.last_outcome == "reflected"
    assert stored.triggered_at
    assert len(service._pending_events) == 1
    assert "周期性自主意向浮现" in service._pending_events[0].content


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


async def test_duplicate_trigger_cannot_overlap_in_flight_speak(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-1"
        return "schedule-1"

    async def fake_wake(intent):
        assert intent.active_occurrence_id.endswith(":1")

    monkeypatch.setattr("plugins.life_engine.service.core.register_autonomy_schedule", fake_schedule)
    monkeypatch.setattr(service, "_wake_stream_for_autonomy", fake_wake)
    receipt = await service.schedule_autonomy_intent(
        kind="speak",
        motivation="稍后重新判断是否开口",
        delay_minutes=2,
        target_stream_id="private-stream",
    )

    first = await service.trigger_autonomy_intent(receipt["intent_id"])
    second = await service.trigger_autonomy_intent(receipt["intent_id"])

    assert first["triggered"] is True
    assert first["occurrence_id"].endswith(":1")
    assert second == {"triggered": False, "reason": "status=in_flight"}


async def test_recurring_speak_waits_for_terminal_receipt_before_next_schedule(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _make_service(tmp_path)
    scheduled: list[str] = []

    async def fake_schedule(_plugin, intent):
        scheduled.append(intent.status)
        intent.schedule_id = f"schedule-{len(scheduled)}"
        return intent.schedule_id

    async def fake_wake(_intent):
        return None

    monkeypatch.setattr("plugins.life_engine.service.core.register_autonomy_schedule", fake_schedule)
    monkeypatch.setattr(service, "_wake_stream_for_autonomy", fake_wake)
    receipt = await service.schedule_autonomy_intent(
        kind="speak",
        motivation="隔一会儿重新判断是否开口",
        delay_minutes=2,
        target_stream_id="private-stream",
        repeat=True,
        max_occurrences=2,
    )
    due = await service.trigger_autonomy_intent(receipt["intent_id"])
    assert scheduled == ["scheduled"]

    stored = AutonomyIntentStore(tmp_path).get(receipt["intent_id"])
    assert stored is not None
    assert stored.status == "in_flight"

    completed = await service.complete_autonomy_occurrences(
        [{"intent_id": receipt["intent_id"], "occurrence_id": due["occurrence_id"]}],
        outcome="passed",
    )
    assert completed == {"completed": 1, "scheduled": 1}
    assert scheduled == ["scheduled", "scheduled"]


async def test_restore_quarantines_legacy_unbounded_recurring_intent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    intent = build_intent(
        kind="reflect",
        motivation="有限地再想几次",
        delay_minutes=2,
        repeat=True,
        max_occurrences=2,
    )
    intent.max_occurrences = 0
    AutonomyIntentStore(tmp_path).upsert(intent)

    class RunningScheduler:
        is_running = True

        async def create_schedule(self, **_kwargs):
            raise AssertionError("legacy unbounded recurrence must not be restored")

    monkeypatch.setattr(
        "plugins.life_engine.autonomy.get_unified_scheduler",
        lambda: RunningScheduler(),
    )

    assert await restore_autonomy_intents(SimpleNamespace(service=None), tmp_path) == 0
    restored = AutonomyIntentStore(tmp_path).get(intent.intent_id)
    assert restored is not None
    assert restored.status == "renewal_required"
    assert "execution lease" in restored.renewal_reason


async def test_autonomy_delivery_claim_enforces_stream_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-1"
        return "schedule-1"

    async def fake_wake(_intent):
        return None

    monkeypatch.setattr("plugins.life_engine.service.core.register_autonomy_schedule", fake_schedule)
    monkeypatch.setattr(service, "_wake_stream_for_autonomy", fake_wake)
    receipt = await service.schedule_autonomy_intent(
        kind="speak",
        motivation="稍后向私聊开口",
        delay_minutes=2,
        target_stream_id="private-stream",
    )
    due = await service.trigger_autonomy_intent(receipt["intent_id"])
    occurrence = {
        "intent_id": receipt["intent_id"],
        "occurrence_id": due["occurrence_id"],
    }

    rejected = await service.claim_autonomy_occurrences(
        [occurrence],
        action_id="call-1",
        target_stream_id="group-stream",
    )
    accepted = await service.claim_autonomy_occurrences(
        [occurrence],
        action_id="call-2",
        target_stream_id="private-stream",
    )

    assert rejected == {"claimed": False, "reason": "cross_stream_not_authorized"}
    assert accepted == {"claimed": True, "count": 1}


async def test_delivery_unknown_never_chains_the_next_occurrence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-1"
        return "schedule-1"

    async def fake_wake(_intent):
        return None

    monkeypatch.setattr("plugins.life_engine.service.core.register_autonomy_schedule", fake_schedule)
    monkeypatch.setattr(service, "_wake_stream_for_autonomy", fake_wake)
    receipt = await service.schedule_autonomy_intent(
        kind="speak",
        motivation="有限次数地重新判断是否开口",
        delay_minutes=2,
        target_stream_id="private-stream",
        repeat=True,
        max_occurrences=3,
    )
    due = await service.trigger_autonomy_intent(receipt["intent_id"])
    occurrence = {
        "intent_id": receipt["intent_id"],
        "occurrence_id": due["occurrence_id"],
    }
    assert (
        await service.claim_autonomy_occurrences(
            [occurrence],
            action_id="call-unknown",
            target_stream_id="private-stream",
        )
    )["claimed"] is True

    completed = await service.complete_autonomy_occurrences(
        [occurrence],
        outcome="delivery_unknown",
        action_id="call-unknown",
    )
    stored = AutonomyIntentStore(tmp_path).get(receipt["intent_id"])

    assert completed == {"completed": 1, "scheduled": 0}
    assert stored is not None
    assert stored.status == "renewal_required"
    assert stored.last_outcome == "delivery_unknown"


def test_autonomy_action_message_identity_is_causal_and_segment_scoped() -> None:
    stream = ChatStream(stream_id="private-stream", platform="qq")
    trigger = Message(
        message_id="autonomy-trigger",
        stream_id="private-stream",
        extra={
            "life_turn_scope": {
                "stream_id": "private-stream",
                "turn_key": "turn-1",
                "autonomy_occurrences": [
                    {
                        "intent_id": "intent-1",
                        "occurrence_id": "intent-1:1",
                        "authorized_stream_id": "private-stream",
                    }
                ],
            }
        },
    )
    action = LifeSendTextAction(stream, SimpleNamespace())
    action._trigger_message = trigger
    action._tool_call_id = "call-1"

    first = action._action_message_id("private-stream", 0)
    replay = action._action_message_id("private-stream", 0)
    second_segment = action._action_message_id("private-stream", 1)
    other_stream = action._action_message_id("group-stream", 0)

    assert first == replay
    assert len({first, second_segment, other_stream}) == 3
    assert action._action_origin_extra()["origin_stream_id"] == "private-stream"


async def test_restore_quarantines_in_flight_occurrence_even_before_scheduler_start(
    tmp_path: Path,
    monkeypatch,
) -> None:
    intent = build_intent(
        kind="speak",
        motivation="稍后再决定是否开口",
        delay_minutes=2,
        target_stream_id="private-stream",
    )
    intent.status = "in_flight"
    intent.occurrence_count = 1
    intent.active_occurrence_id = f"{intent.intent_id}:1"
    intent.active_occurrence_status = "dispatching"
    AutonomyIntentStore(tmp_path).upsert(intent)

    monkeypatch.setattr(
        "plugins.life_engine.autonomy.get_unified_scheduler",
        lambda: SimpleNamespace(is_running=False),
    )

    assert await restore_autonomy_intents(SimpleNamespace(service=None), tmp_path) == 0
    restored = AutonomyIntentStore(tmp_path).get(intent.intent_id)

    assert restored is not None
    assert restored.status == "renewal_required"
    assert restored.active_occurrence_status == "delivery_unknown"
    assert "unfinished occurrence" in restored.renewal_reason
    events = (tmp_path / "autonomy_intent_events.jsonl").read_text(
        encoding="utf-8"
    )
    assert "recovered_delivery_unknown" in events


async def test_renew_extends_existing_occurrence_lease(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-renewed"
        return intent.schedule_id

    class FakeScheduler:
        async def remove_schedule(self, _schedule_id):
            return True

    monkeypatch.setattr(
        "plugins.life_engine.service.core.register_autonomy_schedule",
        fake_schedule,
    )
    monkeypatch.setattr(
        "plugins.life_engine.service.core.get_unified_scheduler",
        lambda: FakeScheduler(),
    )
    receipt = await service.schedule_autonomy_intent(
        kind="reflect",
        motivation="有限次数地回来想想",
        delay_minutes=2,
        repeat=True,
        max_occurrences=10,
    )
    stored = AutonomyIntentStore(tmp_path).get(receipt["intent_id"])
    assert stored is not None
    stored.status = "renewal_required"
    stored.occurrence_count = 3
    AutonomyIntentStore(tmp_path).upsert(stored)

    result = await service.manage_autonomy_intent(
        action="renew",
        intent_id=receipt["intent_id"],
        additional_occurrences=2,
    )

    assert result["status"] == "scheduled"
    assert result["max_occurrences"] == 12


async def test_renew_rejects_an_in_flight_occurrence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-1"
        return intent.schedule_id

    async def fake_wake(_intent):
        return None

    monkeypatch.setattr(
        "plugins.life_engine.service.core.register_autonomy_schedule",
        fake_schedule,
    )
    monkeypatch.setattr(service, "_wake_stream_for_autonomy", fake_wake)
    receipt = await service.schedule_autonomy_intent(
        kind="speak",
        motivation="有限次数地回来判断",
        delay_minutes=2,
        target_stream_id="private-stream",
        repeat=True,
        max_occurrences=3,
    )
    await service.trigger_autonomy_intent(receipt["intent_id"])

    with pytest.raises(ValueError, match="in flight"):
        await service.manage_autonomy_intent(
            action="renew",
            intent_id=receipt["intent_id"],
            additional_occurrences=1,
        )


async def test_cancel_clears_active_occurrence_without_restart_resurrection(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _make_service(tmp_path)

    async def fake_schedule(_plugin, intent):
        intent.schedule_id = "schedule-1"
        return intent.schedule_id

    async def fake_wake(_intent):
        return None

    class FakeScheduler:
        is_running = False

        async def remove_schedule(self, _schedule_id):
            return True

    monkeypatch.setattr(
        "plugins.life_engine.service.core.register_autonomy_schedule",
        fake_schedule,
    )
    monkeypatch.setattr(service, "_wake_stream_for_autonomy", fake_wake)
    monkeypatch.setattr(
        "plugins.life_engine.service.core.get_unified_scheduler",
        lambda: FakeScheduler(),
    )
    receipt = await service.schedule_autonomy_intent(
        kind="speak",
        motivation="稍后重新判断",
        delay_minutes=2,
        target_stream_id="private-stream",
        repeat=True,
        max_occurrences=3,
    )
    due = await service.trigger_autonomy_intent(receipt["intent_id"])

    cancelled = await service.manage_autonomy_intent(
        action="cancel",
        intent_id=receipt["intent_id"],
    )
    assert cancelled["status"] == "cancelled"
    stored = AutonomyIntentStore(tmp_path).get(receipt["intent_id"])
    assert stored is not None
    assert stored.active_occurrence_id == ""
    assert stored.last_occurrence_id == due["occurrence_id"]

    monkeypatch.setattr(
        "plugins.life_engine.autonomy.get_unified_scheduler",
        lambda: FakeScheduler(),
    )
    assert await restore_autonomy_intents(SimpleNamespace(service=None), tmp_path) == 0
    restored = AutonomyIntentStore(tmp_path).get(receipt["intent_id"])
    assert restored is not None
    assert restored.status == "cancelled"
