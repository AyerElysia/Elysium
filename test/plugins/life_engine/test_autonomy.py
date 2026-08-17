"""Read-only compatibility contracts for the retired AutonomyIntent system."""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.autonomy import (
    AutonomyIntentStore,
    build_intent,
    restore_autonomy_intents,
    schedule_autonomy_intent,
)
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.tools.autonomy_tools import (
    LifeEngineManageAutonomyIntentTool,
    LifeEngineScheduleAutonomyIntentTool,
)
from src.core.config.core_config import CoreConfig


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


def test_legacy_snapshot_model_remains_parseable() -> None:
    intent = build_intent(
        kind="reflect",
        motivation="旧快照中的原始动机",
        delay_minutes=5,
        min_delay_minutes=1,
        max_delay_minutes=60,
    )
    restored = type(intent).from_dict(intent.to_dict())
    assert restored.intent_id == intent.intent_id
    assert restored.motivation == "旧快照中的原始动机"
    assert restored.status == "scheduled"


def test_legacy_snapshot_validation_is_preserved() -> None:
    with pytest.raises(ValueError, match="delay_minutes"):
        build_intent(
            kind="reflect",
            motivation="不合法的旧快照夹具",
            delay_minutes=120,
            min_delay_minutes=1,
            max_delay_minutes=60,
        )
    with pytest.raises(ValueError, match="max_occurrences or lease_minutes"):
        build_intent(
            kind="speak",
            motivation="旧周期快照缺少租约",
            delay_minutes=2,
            repeat=True,
        )


@pytest.mark.asyncio
async def test_legacy_scheduler_entry_is_fail_closed(monkeypatch) -> None:
    intent = build_intent(
        kind="reflect",
        motivation="不会再进入调度器",
        delay_minutes=5,
        min_delay_minutes=1,
        max_delay_minutes=60,
    )
    touched = False

    def _forbidden_scheduler() -> object:
        nonlocal touched
        touched = True
        raise AssertionError("legacy scheduler must not be opened")

    monkeypatch.setattr(
        "plugins.life_engine.autonomy.get_unified_scheduler",
        _forbidden_scheduler,
    )
    with pytest.raises(RuntimeError, match="LegacyAutonomyReadOnly"):
        await schedule_autonomy_intent(SimpleNamespace(), intent)
    assert touched is False


@pytest.mark.asyncio
async def test_legacy_restore_reads_bytes_without_rewrite_or_schedule(
    tmp_path: Path,
    monkeypatch,
) -> None:
    intent = build_intent(
        kind="speak",
        motivation="旧数据必须原样保留",
        delay_minutes=5,
        min_delay_minutes=1,
        max_delay_minutes=60,
        target_stream_id="legacy-stream",
    )
    store = AutonomyIntentStore(tmp_path)
    store.upsert(intent)
    before = store.path.read_bytes()
    before_hash = hashlib.sha256(before).hexdigest()

    monkeypatch.setattr(
        "plugins.life_engine.autonomy.get_unified_scheduler",
        lambda: (_ for _ in ()).throw(
            AssertionError("read-only restore must not inspect scheduler state")
        ),
    )
    assert await restore_autonomy_intents(SimpleNamespace(), tmp_path) == 0
    after = store.path.read_bytes()
    assert after == before
    assert hashlib.sha256(after).hexdigest() == before_hash


@pytest.mark.asyncio
async def test_service_rejects_legacy_creation_without_writing(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    with pytest.raises(RuntimeError, match="LegacyAutonomyReadOnly"):
        await service.schedule_autonomy_intent(
            kind="speak",
            motivation="不会创建",
            delay_minutes=5,
            target_key="p-legacy",
        )
    assert not AutonomyIntentStore(tmp_path).path.exists()


@pytest.mark.asyncio
async def test_legacy_tools_report_read_only_without_calling_service(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _make_service(tmp_path)
    called = False

    async def _forbidden(**_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("retired creation must not call the service")

    monkeypatch.setattr(service, "schedule_autonomy_intent", _forbidden)
    create_tool = LifeEngineScheduleAutonomyIntentTool(plugin=service.plugin)
    ok, result = await create_tool.execute(
        kind="silence",
        motivation="旧调用",
        delay_minutes=2,
    )
    assert ok is False
    assert isinstance(result, dict)
    assert result["error"] == "LegacyAutonomyReadOnly"
    assert called is False

    manage_tool = LifeEngineManageAutonomyIntentTool(plugin=service.plugin)
    ok, result = await manage_tool.execute(action="cancel", intent_id="legacy")
    assert ok is False
    assert isinstance(result, dict)
    assert result["error"] == "LegacyAutonomyReadOnly"


@pytest.mark.asyncio
async def test_legacy_trigger_and_receipts_cannot_mutate(tmp_path: Path) -> None:
    service = _make_service(tmp_path)
    assert await service.trigger_autonomy_intent("legacy") == {
        "triggered": False,
        "reason": "legacy_autonomy_read_only",
        "intent_id": "legacy",
    }
    assert await service.claim_autonomy_occurrences(
        [{"intent_id": "legacy", "occurrence_id": "old"}],
        action_id="action",
        target_stream_id="legacy-stream",
    ) == {
        "claimed": False,
        "count": 0,
        "reason": "legacy_autonomy_read_only",
    }
    assert await service.complete_autonomy_occurrences(
        [{"intent_id": "legacy", "occurrence_id": "old"}],
        outcome="sent",
    ) == {
        "completed": 0,
        "scheduled": 0,
        "reason": "legacy_autonomy_read_only",
    }
