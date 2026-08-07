"""life heartbeat self-pause tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.tools import LifeEngineRestHeartbeatTool
from src.core.config.core_config import CoreConfig
from src.core.models.message import Message


def _service(workspace_path: Path | None = None) -> LifeEngineService:
    config = LifeEngineConfig()
    if workspace_path is not None:
        config.settings.workspace_path = str(workspace_path)
    return LifeEngineService(
        SimpleNamespace(
            config=config,
            global_storage_config=CoreConfig(
                storage=CoreConfig.StorageSection(backend="local")
            ),
        )
    )


def test_external_silence_no_longer_exposes_pause_mechanism() -> None:
    service = _service()
    service._state.last_external_message_at = (
        datetime.now(timezone.utc).astimezone() - timedelta(hours=6)
    ).isoformat()

    snapshot = service.snapshot()

    assert not hasattr(service, "_should_pause_llm_heartbeat_for_external_silence")
    assert not hasattr(service, "get_external_silence_pause_status")
    assert not hasattr(service._cfg().settings, "idle_pause_after_external_silence_minutes")
    assert snapshot["external_silence_minutes"] is not None
    assert "llm_heartbeat_paused_by_external_silence" not in snapshot
    assert "external_silence_pause_threshold_minutes" not in snapshot


def test_request_self_pause_clamps_and_sets_status(tmp_path: Path) -> None:
    service = _service(workspace_path=tmp_path)

    result = asyncio.run(
        service.request_self_pause(
            duration_minutes=999,
            reason="想安静整理一下",
        )
    )

    assert result["paused"] is True
    assert result["duration_minutes"] == 480
    assert result["requested_minutes"] == 999

    status = service.get_self_pause_status()
    assert status["paused"] is True
    assert status["duration_minutes"] == 480
    assert status["reason"] == "想安静整理一下"
    assert status["will_wake_on_external_message"] is True


def test_self_pause_expires() -> None:
    service = _service()
    service._state.self_pause_until = (
        datetime.now(timezone.utc).astimezone() - timedelta(minutes=1)
    ).isoformat()
    service._state.self_pause_reason = "短暂休息"
    service._state.self_pause_duration_minutes = 5

    status = service.get_self_pause_status()

    assert status["paused"] is False
    assert status["remaining_minutes"] == 0
    assert status["reason"] == "短暂休息"


def test_received_message_unlocks_self_pause_but_sent_message_does_not(tmp_path: Path) -> None:
    service = _service(workspace_path=tmp_path)
    asyncio.run(
        service.request_self_pause(
            duration_minutes=30,
            reason="休息一下",
        )
    )
    message = Message(
        message_id="msg-1",
        content="醒醒",
        sender_id="user-1",
        sender_name="Ayer",
        platform="qq",
        chat_type="private",
        stream_id="stream-1",
    )

    asyncio.run(service.record_message(message, direction="sent"))
    assert service.get_self_pause_status()["paused"] is True

    asyncio.run(service.record_message(message, direction="received"))
    assert service.get_self_pause_status()["paused"] is False
    assert service._state.self_pause_until is None


def test_self_pause_persists_across_restart(tmp_path: Path) -> None:
    service = _service(workspace_path=tmp_path)
    asyncio.run(
        service.request_self_pause(
            duration_minutes=20,
            reason="重启后也应该继续休息",
        )
    )

    restored = _service(workspace_path=tmp_path)
    asyncio.run(restored._load_runtime_context())

    status = restored.get_self_pause_status()
    assert status["paused"] is True
    assert status["duration_minutes"] == 20
    assert status["reason"] == "重启后也应该继续休息"


def test_rest_heartbeat_tool_requests_self_pause(
    tmp_path: Path,
    monkeypatch,
) -> None:
    service = _service(workspace_path=tmp_path)
    monkeypatch.setattr(
        "plugins.life_engine.tools.rest_tools.get_life_engine_service",
        lambda: service,
    )

    tool = LifeEngineRestHeartbeatTool(plugin=object())
    ok, result = asyncio.run(
        tool.execute(
            duration_minutes=10,
            reason="现在想停下来听一会儿安静",
        )
    )

    assert ok is True
    assert isinstance(result, dict)
    assert result["duration_minutes"] == 10
    assert result["will_wake_on_external_message"] is True
    assert service.get_self_pause_status()["paused"] is True
