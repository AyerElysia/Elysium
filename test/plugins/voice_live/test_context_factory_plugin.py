from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.context_bridge import ContextBridge, _compact_context_lines
from plugins.voice_live.life_binding import get_running_life_service
from plugins.voice_live.plugin import VoiceLivePlugin
from plugins.voice_live.providers.factory import create_provider
from plugins.voice_live.providers.minicpm_omni import MiniCPMOmniProvider
from plugins.voice_live.providers.openai_realtime import OpenAIRealtimeProvider
from plugins.voice_live.providers.qwen_realtime import QwenRealtimeProvider
from plugins.voice_live.runtime_store import VoiceEpisodeStore


def _core_config() -> Any:
    return SimpleNamespace(
        personality=SimpleNamespace(
            nickname="爱莉",
            alias_names=["Elysia"],
            personality_core="温柔而自由",
            personality_side="好奇",
            identity="Elysium 的意识",
            background_story="完整背景",
            reply_style="自然口语",
            safety_guidelines=["尊重意志"],
            negative_behaviors=["不冒充"],
        )
    )


class FakeConsciousness:
    instance_id = "voice_live_episode"
    stream_id = "voice_live_episode"

    def prepare_perception(self) -> Any:
        return SimpleNamespace(content='{"scene":"voice"}')


class FakeLifeService:
    def __init__(self) -> None:
        self.messages: list[tuple[Any, str]] = []

    async def record_message(self, message: Any, *, direction: str) -> None:
        self.messages.append((message, direction))


@pytest.mark.asyncio
async def test_context_bridge_separates_stable_identity_and_transient_world(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = VoiceLiveConfig()
    config.full_duplex.instructions = "保留她自己的判断。"
    store = VoiceEpisodeStore(tmp_path, "voice_live_episode", "episode")
    consciousness = FakeConsciousness()
    bridge = ContextBridge(config, consciousness, store)
    monkeypatch.setattr(
        "plugins.voice_live.context_bridge.get_core_config", _core_config
    )
    service = FakeLifeService()
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service",
        lambda: service,
    )

    await store.append_async(
        "transcript.final", {"role": "user", "text": "此前的完整历史"}
    )
    prompt = bridge.build_system_prompt()
    assert all(
        value in prompt
        for value in ("爱莉", "完整背景", "此前的完整历史", "自己的判断")
    )
    assert "scene" not in prompt
    transient, prepared = bridge.build_llm_context_prefix()
    assert "scene" in transient
    assert prepared is not None

    await bridge.record_transcript("user", "你好", provider_event_id="u1")
    await bridge.record_transcript("assistant", "你好呀", provider_event_id="a1")
    assert [direction for _, direction in service.messages] == ["received", "sent"]
    assert service.messages[0][0].stream_id == consciousness.stream_id
    assert service.messages[1][0].sender_id == consciousness.instance_id
    await bridge.record_transcript("assistant", "")
    with pytest.raises(ValueError):
        await bridge.record_transcript("system", "invalid")


def test_realtime_perception_projection_is_bounded_and_traceable() -> None:
    content = (
        "presence\n"
        + "\n".join(f"- assertion-{index}: {'世界' * 40}" for index in range(2_000))
        + "\nlatest-change"
    )

    projected, stats = _compact_context_lines(content, 4096)

    assert len(projected.encode("utf-8")) <= 4096
    assert projected.startswith("presence")
    assert projected.endswith("latest-change")
    assert "LifeEngine" in projected
    assert "inner_query" in projected
    assert stats["compacted"] is True
    assert stats["original_bytes"] > stats["delivered_bytes"]
    assert stats["omitted_lines"] > 0


@pytest.mark.asyncio
async def test_context_bridge_honors_optional_life_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = VoiceLiveConfig()
    config.session.record_to_life = True
    config.session.require_life_engine = False
    store = VoiceEpisodeStore(tmp_path, "voice_live_optional", "optional")
    bridge = ContextBridge(config, FakeConsciousness(), store)
    monkeypatch.setattr(
        "plugins.life_engine.service.registry.get_life_engine_service", lambda: None
    )
    await bridge.record_transcript("user", "still durable")
    assert store.transcript()[-1]["text"] == "still durable"
    config.session.require_life_engine = True
    with pytest.raises(RuntimeError):
        await bridge.record_transcript("assistant", "must fail")


def test_factory_is_explicit_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = VoiceLiveConfig()
    assert isinstance(create_provider(config), MiniCPMOmniProvider)

    monkeypatch.setenv("VOICE_LIVE_API_KEY", "runtime-secret")
    config.full_duplex.provider_type = "qwen_realtime"
    config.full_duplex.upstream_url = "wss://example.test/realtime"
    config.full_duplex.model_name = "qwen3.5-omni-plus-realtime"
    assert isinstance(create_provider(config), QwenRealtimeProvider)

    config.full_duplex.provider_type = "openai_realtime"
    config.full_duplex.model_name = "gpt-realtime"
    assert isinstance(create_provider(config), OpenAIRealtimeProvider)

    config.full_duplex.provider_type = "disabled"
    with pytest.raises(RuntimeError, match="disabled"):
        create_provider(config)
    config.full_duplex.provider_type = "minicpm_omni"
    config.full_duplex.upstream_url = ""
    with pytest.raises(RuntimeError, match="upstream_url"):
        create_provider(config)


def test_factory_reads_owner_only_api_key_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = tmp_path / "qwen.key"
    secret.write_text("runtime-secret\n", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.delenv("VOICE_LIVE_API_KEY", raising=False)
    config = VoiceLiveConfig()
    config.full_duplex.provider_type = "qwen_realtime"
    config.full_duplex.upstream_url = "wss://example.test/realtime"
    config.full_duplex.api_key_file = str(secret)

    assert isinstance(create_provider(config), QwenRealtimeProvider)


def test_plugin_declares_only_its_router_and_event_handler() -> None:
    plugin = VoiceLivePlugin(VoiceLiveConfig())
    components = plugin.get_components()
    assert [component.__name__ for component in components] == [
        "VoiceLiveRouter",
        "VoiceLiveEventHandler",
    ]


def test_life_binding_prefers_plugin_loader_package_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_service = object()
    root = types.ModuleType("life_engine")
    root.__path__ = []  # type: ignore[attr-defined]
    service_package = types.ModuleType("life_engine.service")
    service_package.__path__ = []  # type: ignore[attr-defined]
    registry = types.ModuleType("life_engine.service.registry")
    registry.get_life_engine_service = lambda: runtime_service  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "life_engine", root)
    monkeypatch.setitem(sys.modules, "life_engine.service", service_package)
    monkeypatch.setitem(sys.modules, "life_engine.service.registry", registry)

    assert get_running_life_service() is runtime_service
