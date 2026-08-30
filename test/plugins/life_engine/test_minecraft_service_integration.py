"""Generic Life Engine registration and lifecycle contracts for Minecraft."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import plugins.life_engine.service.core as core_module
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.plugin import LifeEnginePlugin
from plugins.life_engine.service.core import LifeEngineService


@dataclass
class _DummyPlugin:
    config: LifeEngineConfig


class _FakeMinecraftSession:
    def __init__(self, *, fail_close: bool = False) -> None:
        self.close_calls = 0
        self.fail_close = fail_close

    async def close(self) -> None:
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("injected Minecraft close failure")


class _FakeConsumer:
    def __init__(self) -> None:
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


def _config(
    tmp_path: Path, *, minecraft: bool, learning: bool = True
) -> LifeEngineConfig:
    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    config.minecraft.enabled = minecraft
    config.learning.enabled = learning
    return config


def _write_subject_authority(tmp_path: Path) -> None:
    """Create the complete test-only authority required by service startup."""

    for name in ("SOUL.md", "USER.md", "MEMORY.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")


def test_minecraft_tool_registration_follows_minecraft_config(tmp_path: Path) -> None:
    enabled = LifeEnginePlugin(_config(tmp_path, minecraft=True)).get_components()
    disabled = LifeEnginePlugin(_config(tmp_path, minecraft=False)).get_components()

    enabled_names = {str(getattr(component, "tool_name", "")) for component in enabled}
    disabled_names = {
        str(getattr(component, "tool_name", "")) for component in disabled
    }
    assert "nucleus_minecraft" in enabled_names
    assert "nucleus_minecraft" not in disabled_names


def test_manifest_declares_stable_minecraft_tool_signature() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[3]
        / "plugins"
        / "life_engine"
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    matches = [
        item
        for item in manifest["include"]
        if item.get("component_type") == "tool"
        and item.get("component_name") == "nucleus_minecraft"
    ]
    assert matches == [
        {
            "component_type": "tool",
            "component_name": "nucleus_minecraft",
            "dependencies": [],
            "enabled": True,
        }
    ]


def test_minecraft_session_uses_recent_subconscious_provider(
    tmp_path: Path,
) -> None:
    """The service wires its bounded read-only continuity API into Minecraft."""

    service = LifeEngineService(_DummyPlugin(_config(tmp_path, minecraft=True)))

    session = service._create_minecraft_session()

    callback = session._get_recent_subconscious_context
    assert callback.__self__ is service
    assert callback.__func__ is LifeEngineService.get_recent_subconscious_context

    subject_callback = session._get_subject_context_projection_snapshot
    assert subject_callback.__self__ is service
    assert (
        subject_callback.__func__
        is LifeEngineService.get_subject_context_projection_snapshot
    )

    record_callback = session._record_minecraft_consciousness_decision
    assert record_callback.__self__ is service
    assert (
        record_callback.__func__
        is LifeEngineService.record_minecraft_consciousness_decision
    )


async def test_minecraft_decision_record_retry_is_exact_and_not_duplicated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw-write ambiguity retries the same event before body execution."""

    service = LifeEngineService(_DummyPlugin(_config(tmp_path, minecraft=True)))
    published: list[Any] = []
    saves = 0

    async def publish(events: list[Any]) -> None:
        published.append(events[0])
        if len(published) == 1:
            raise OSError("temporary event backend failure")

    async def save(*_: Any, **__: Any) -> None:
        nonlocal saves
        saves += 1

    monkeypatch.setattr(service, "_publish_raw_events", publish)
    monkeypatch.setattr(service, "_save_runtime_context", save)
    decision = {
        "schema": "minecraft.consciousness_decision.v1",
        "decision_id": "minecraft_decision_" + "a" * 64,
        "kind": "pursue",
        "turn_index": 1,
        "authored_at": "2026-08-30T12:00:00+00:00",
        "intention": "去看看山后面",
        "reason": "我想知道那里有什么",
        "reconsider_after_seconds": None,
    }
    context = {
        "schema": "minecraft.consciousness_turn_reference.v1",
        "session_id": "session-1",
        "stream_id": "game.minecraft.session-1",
        "instance_id": "minecraft-session-1",
        "body_name": "agent",
        "turn_index": 1,
        "wake_reasons": ["session_started"],
        "subject": {"projection_sha256": "b" * 64},
        "perception": {"observation": {"observation_id": "observation-1"}},
        "recent_outcome_decision_ids": [],
    }

    with pytest.raises(OSError, match="temporary event backend failure"):
        await service.record_minecraft_consciousness_decision(decision, context)
    event = await service.record_minecraft_consciousness_decision(decision, context)
    replayed = await service.record_minecraft_consciousness_decision(decision, context)

    assert published == [event, event]
    assert replayed is event
    assert saves == 1
    assert [item.event_id for item in service._pending_events] == [event.event_id]
    assert event.source_instance_id == "minecraft-session-1"
    assert event.occurrence_id == decision["decision_id"]
    assert "去看看山后面" in str(event.raw_content)


async def test_minecraft_session_initializes_when_learning_is_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_subject_authority(tmp_path)
    config = _config(tmp_path, minecraft=True, learning=False)
    config.autonomy.enabled = False
    config.streams.enabled = False
    config.drives.enabled = False
    config.memory_index.enabled = False
    config.memory_witness.enabled = False
    service = LifeEngineService(_DummyPlugin(config))
    session = _FakeMinecraftSession()
    monkeypatch.setattr(service, "_create_minecraft_session", lambda: session)

    await service.start()

    assert service.minecraft_session is session
    assert service._learning_scheduler is None

    await service.stop()

    assert session.close_calls == 1
    assert service.minecraft_session is None


async def test_minecraft_disabled_does_not_construct_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LifeEngineService(_DummyPlugin(_config(tmp_path, minecraft=False)))

    def _unexpected_create() -> Any:
        raise AssertionError("disabled Minecraft must not construct a session")

    monkeypatch.setattr(service, "_create_minecraft_session", _unexpected_create)
    await service._initialize_minecraft_session()
    assert service.minecraft_session is None


async def test_minecraft_close_is_idempotent(tmp_path: Path) -> None:
    service = LifeEngineService(_DummyPlugin(_config(tmp_path, minecraft=True)))
    session = _FakeMinecraftSession()
    service._minecraft_session = session

    await service._close_minecraft_session()
    await service._close_minecraft_session()

    assert session.close_calls == 1
    assert service.minecraft_session is None


async def test_partial_start_closes_acquired_minecraft_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LifeEngineService(
        _DummyPlugin(_config(tmp_path, minecraft=True, learning=False))
    )
    session = _FakeMinecraftSession()
    monkeypatch.setattr(service, "_create_minecraft_session", lambda: session)

    async def _partial_start() -> None:
        await service._initialize_minecraft_session()
        raise RuntimeError("failure after Minecraft acquisition")

    async def _no_op(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(service, "_start_impl", _partial_start)
    monkeypatch.setattr(service, "_save_runtime_context", _no_op)
    monkeypatch.setattr(service, "_close_selected_storage", _no_op)
    monkeypatch.setattr(core_module, "cleanup_autonomy_schedules", _no_op)

    with pytest.raises(RuntimeError, match="failure after Minecraft acquisition"):
        await service.start()

    assert session.close_calls == 1
    assert service.minecraft_session is None


async def test_stop_reports_minecraft_failure_and_closes_other_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = LifeEngineService(_DummyPlugin(_config(tmp_path, minecraft=True)))
    minecraft = _FakeMinecraftSession(fail_close=True)
    learning = _FakeConsumer()
    memory = _FakeConsumer()
    selected_close_calls = 0
    service._minecraft_session = minecraft
    service._learning_scheduler = learning
    service._memory_service = memory  # type: ignore[assignment]

    async def _no_op(*_: Any, **__: Any) -> None:
        return None

    async def _close_selected() -> None:
        nonlocal selected_close_calls
        selected_close_calls += 1

    monkeypatch.setattr(service, "_save_runtime_context", _no_op)
    monkeypatch.setattr(service, "_close_selected_storage", _close_selected)
    monkeypatch.setattr(core_module, "cleanup_autonomy_schedules", _no_op)

    with pytest.raises(ExceptionGroup) as captured:
        await service.stop()

    assert [str(exc) for exc in captured.value.exceptions] == [
        "injected Minecraft close failure"
    ]
    assert minecraft.close_calls == 1
    assert service.minecraft_session is minecraft
    assert learning.close_calls == 1
    assert memory.close_calls == 1
    assert selected_close_calls == 1
