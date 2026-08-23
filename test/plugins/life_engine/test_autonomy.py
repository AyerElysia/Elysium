"""Read-only compatibility contracts for the retired AutonomyIntent system."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.autonomy import (
    AsyncLocalAutonomyIntentStore,
    AutonomyIntentStore,
    LegacyAutonomyReadOnly,
    SelectedAutonomyIntentStore,
    build_intent,
    restore_autonomy_intents,
    schedule_autonomy_intent,
)
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import LifeEngineService
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
    store.path.write_text(
        json.dumps(
            {
                "version": 3,
                "updated_at": intent.updated_at,
                "intents": [intent.to_dict()],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
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
async def test_all_legacy_store_mutations_fail_closed(tmp_path: Path) -> None:
    intent = build_intent(
        kind="reflect",
        motivation="只用于验证退役写入口",
        delay_minutes=5,
    )
    local = AutonomyIntentStore(tmp_path)
    with pytest.raises(LegacyAutonomyReadOnly):
        local.save([intent])
    with pytest.raises(LegacyAutonomyReadOnly):
        local.upsert(intent)
    with pytest.raises(LegacyAutonomyReadOnly):
        local.append_event("triggered", intent)
    assert not local.path.exists()

    async_local = AsyncLocalAutonomyIntentStore(tmp_path)
    with pytest.raises(LegacyAutonomyReadOnly):
        await async_local.save([intent])
    with pytest.raises(LegacyAutonomyReadOnly):
        await async_local.upsert(intent)
    with pytest.raises(LegacyAutonomyReadOnly):
        await async_local.append_event("triggered", intent)

    selected = SelectedAutonomyIntentStore(SimpleNamespace())
    with pytest.raises(LegacyAutonomyReadOnly):
        await selected.save([intent])
    with pytest.raises(LegacyAutonomyReadOnly):
        await selected.upsert(intent)
    with pytest.raises(LegacyAutonomyReadOnly):
        await selected.append_event("triggered", intent)


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
