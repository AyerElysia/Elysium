from __future__ import annotations

import hashlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.voice_live.config import VoiceLiveConfig
from plugins.voice_live.context_bridge import (
    ContextBridge,
    VoicePromptBundle,
    _compact_context_lines,
    _project_episode_transcript,
)
from plugins.voice_live.life_binding import get_running_life_service
from plugins.voice_live.plugin import VoiceLivePlugin
from plugins.voice_live.providers.factory import create_provider
from plugins.voice_live.providers.minicpm_omni import MiniCPMOmniProvider
from plugins.voice_live.providers.openai_realtime import OpenAIRealtimeProvider
from plugins.voice_live.providers.qwen_realtime import QwenRealtimeProvider
from plugins.voice_live.runtime_store import VoiceEpisodeStore


def _subject_snapshot(
    *,
    max_bytes: int,
    source_digest: str | None = None,
    suffix: str = "",
) -> dict[str, Any]:
    contents = {
        "SOUL.md": f"统一的爱莉身份{suffix}",
        "USER.md": f"用户赩汐与关系边界{suffix}",
        "MEMORY.md": f"共同经历与连续记忆{suffix}",
    }
    text = "\n".join(
        f'<subject-source path="{path}">\n{content}\n</subject-source>'
        for path, content in contents.items()
    )
    sources = []
    coverage: dict[str, dict[str, int]] = {}
    for path, content in contents.items():
        content_bytes = len(content.encode("utf-8"))
        sources.append(
            {
                "path": path,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "size_bytes": content_bytes,
            }
        )
        coverage[path] = {
            "original_bytes": content_bytes,
            "delivered_bytes": content_bytes,
            "max_delivered_bytes": max_bytes // 3,
        }
    digest = source_digest or hashlib.sha256(
        "".join(contents.values()).encode("utf-8")
    ).hexdigest()
    return {
        "text": text,
        "schema_version": 1,
        "kind": "derived_subject_context_projection",
        "authority": "derived_non_authoritative",
        "projection_profile": "voice_live",
        "projection_algorithm": "llm_semantic_subject_continuity",
        "projection_version": 1,
        "source_digest": digest,
        "sources": sources,
        "projection_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "budget": {
            "max_bytes": max_bytes,
            "original_bytes": sum(item["size_bytes"] for item in sources),
            "delivered_bytes": len(text.encode("utf-8")),
            "sources": coverage,
        },
    }


def _subconscious_projection(
    content: str = "【潜意识近期上下文】\n最近想到要继续理解这件事",
) -> SimpleNamespace:
    encoded = content.encode("utf-8")
    return SimpleNamespace(
        content=content,
        event_ids=("heartbeat-41",),
        from_sequence=41,
        through_sequence=41,
        group_count=1,
        source_group_count=1,
        omitted_group_count=0,
        delivered_bytes=len(encoded),
        projection_sha256=hashlib.sha256(encoded).hexdigest(),
        algorithm_version="subconscious-recent-causal-groups-v1",
        truncated=False,
    )


class FakeConsciousness:
    instance_id = "voice_live_episode"
    stream_id = "voice_live_episode"


class FakeLifeService:
    def __init__(
        self,
        snapshot: dict[str, Any] | None = None,
        subconscious: SimpleNamespace | None = None,
    ) -> None:
        self.messages: list[tuple[Any, str]] = []
        self.snapshot = snapshot
        self.subconscious = subconscious or _subconscious_projection()
        self.projection_calls: list[dict[str, Any]] = []
        self.subconscious_calls: list[dict[str, Any]] = []
        self.model_turns: list[dict[str, Any]] = []
        self.tool_results: list[dict[str, Any]] = []

    async def get_subject_context_projection_snapshot(
        self, **kwargs: Any
    ) -> dict[str, Any]:
        self.projection_calls.append(dict(kwargs))
        if self.snapshot is None:
            raise RuntimeError("subject projection unavailable")
        return self.snapshot

    async def get_recent_subconscious_context(self, **kwargs: Any) -> Any:
        self.subconscious_calls.append(dict(kwargs))
        return self.subconscious

    async def record_message(self, message: Any, *, direction: str) -> None:
        self.messages.append((message, direction))

    async def record_conscious_model_turn(self, **kwargs: Any) -> dict[str, str]:
        self.model_turns.append(dict(kwargs))
        calls = list(kwargs["calls"])
        if not calls:
            return {}
        call_id = str(calls[0]["call_id"])
        return {call_id: "voice-activity-1"}

    async def record_conscious_tool_results(self, **kwargs: Any) -> None:
        self.tool_results.append(dict(kwargs))


@pytest.mark.asyncio
async def test_context_bridge_separates_identity_and_recent_subconscious_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = VoiceLiveConfig()
    config.full_duplex.instructions = "保留她自己的判断。"
    store = VoiceEpisodeStore(tmp_path, "voice_live_episode", "episode")
    consciousness = FakeConsciousness()
    bridge = ContextBridge(config, consciousness, store)
    service = FakeLifeService(
        _subject_snapshot(max_bytes=config.session.subject_context_max_bytes)
    )
    monkeypatch.setattr(
        "plugins.voice_live.context_bridge.get_running_life_service",
        lambda: service,
    )

    await store.append_async(
        "transcript.final", {"role": "user", "text": "此前的完整历史"}
    )
    bundle = await bridge.build_system_prompt()
    assert isinstance(bundle, VoicePromptBundle)
    prompt = bundle.text
    assert all(
        value in prompt
        for value in (
            "统一的爱莉身份",
            "用户赩汐与关系边界",
            "共同经历与连续记忆",
            "此前的完整历史",
            "自己的判断",
        )
    )
    assert "完整背景" not in prompt
    assert "最近想到要继续理解这件事" not in prompt
    bound = [
        record for record in store.read_all() if record.event == "subject_context.bound"
    ][-1]
    assert "text" not in bound.payload
    assert [source["path"] for source in bound.payload["sources"]] == [
        "SOUL.md",
        "USER.md",
        "MEMORY.md",
    ]
    transient, projection = await bridge.build_llm_context_prefix()
    assert "recent_subconscious_context" in transient
    assert "最近想到要继续理解这件事" in transient
    assert "transient_world_perception" not in transient
    assert projection is service.subconscious
    assert service.subconscious_calls[-1]["max_bytes"] < (
        config.session.perception_context_max_bytes
    )
    stats = bridge.dynamic_context_projection_stats()
    assert stats["projection_kind"] == "recent_subconscious_context"
    assert stats["through_sequence"] == 41
    assert stats["event_count"] == 1

    await bridge.record_transcript("user", "你好", provider_event_id="u1")
    await bridge.record_transcript("assistant", "你好呀", provider_event_id="a1")
    assert [direction for _, direction in service.messages] == ["received", "sent"]
    assert service.messages[0][0].stream_id == consciousness.stream_id
    assert service.messages[1][0].sender_id == consciousness.instance_id
    assistant_episode_record = [
        record
        for record in store.read_all()
        if record.event == "transcript.final"
        and record.payload.get("role") == "assistant"
        and record.payload.get("provider_event_id") == "a1"
    ][-1]
    assert service.model_turns == [
        {
            "stream_id": consciousness.stream_id,
            "source_instance_id": consciousness.instance_id,
            "turn_occurrence_id": (
                "voice:episode:transcript:"
                f"{assistant_episode_record.sequence}"
            ),
            "transport_request_id": "a1",
            "provider_reasoning_content": "",
            "assistant_message": "你好呀",
            "calls": [],
            "surface": "voice_live",
        }
    ]
    await bridge.record_transcript("assistant", "")
    with pytest.raises(ValueError):
        await bridge.record_transcript("system", "invalid")


@pytest.mark.asyncio
async def test_voice_tool_activity_keeps_full_args_and_result_in_shared_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = VoiceLiveConfig()
    config.session.record_to_life = True
    config.session.require_life_engine = True
    store = VoiceEpisodeStore(tmp_path, "voice_tool_activity", "episode")
    consciousness = FakeConsciousness()
    service = FakeLifeService()
    monkeypatch.setattr(
        "plugins.voice_live.context_bridge.get_running_life_service",
        lambda: service,
    )
    bridge = ContextBridge(config, consciousness, store)

    turn_id, activity_ids = await bridge.record_tool_call_activity(
        call_id="voice-call-1",
        name="action-life_send_text",
        arguments_json=json.dumps(
            {
                "mood": "温柔",
                "decision": "回应她",
                "expected_response": "她知道我听见了",
                "thought": "这是我这一刻真实形成的想法",
                "content": "我听见啦",
            },
            ensure_ascii=False,
        ),
    )
    exact_result = {"success": True, "blob": "完整结果" * 5000}
    await bridge.record_tool_result_activity(
        call_id="voice-call-1",
        name="action-life_send_text",
        result=exact_result,
        success=True,
        turn_occurrence_id=turn_id,
        activity_ids=activity_ids,
    )

    assert service.model_turns[0]["surface"] == "voice_live"
    assert service.model_turns[0]["source_instance_id"] == consciousness.instance_id
    assert service.model_turns[0]["calls"][0]["arguments"]["thought"] == (
        "这是我这一刻真实形成的想法"
    )
    assert service.tool_results[0]["surface"] == "voice_live"
    assert service.tool_results[0]["results"][0]["result"] == exact_result


@pytest.mark.asyncio
async def test_context_bridge_rejects_changed_subconscious_projection_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = VoiceLiveConfig()
    projection = _subconscious_projection()
    projection.projection_sha256 = "0" * 64
    service = FakeLifeService(
        _subject_snapshot(max_bytes=config.session.subject_context_max_bytes),
        projection,
    )
    monkeypatch.setattr(
        "plugins.voice_live.context_bridge.get_running_life_service",
        lambda: service,
    )
    bridge = ContextBridge(
        config,
        FakeConsciousness(),
        VoiceEpisodeStore(tmp_path, "voice_invalid_recent", "episode"),
    )

    with pytest.raises(RuntimeError, match="哈希身份"):
        await bridge.build_llm_context_prefix()


@pytest.mark.asyncio
async def test_context_bridge_accepts_empty_recent_subconscious_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = VoiceLiveConfig()
    empty = SimpleNamespace(
        content="",
        event_ids=(),
        from_sequence=0,
        through_sequence=0,
        group_count=0,
        source_group_count=0,
        omitted_group_count=0,
        delivered_bytes=0,
        projection_sha256=hashlib.sha256(b"").hexdigest(),
        algorithm_version="subconscious-recent-causal-groups-v1",
        truncated=False,
    )
    service = FakeLifeService(
        _subject_snapshot(max_bytes=config.session.subject_context_max_bytes),
        empty,
    )
    monkeypatch.setattr(
        "plugins.voice_live.context_bridge.get_running_life_service",
        lambda: service,
    )
    bridge = ContextBridge(
        config,
        FakeConsciousness(),
        VoiceEpisodeStore(tmp_path, "voice_empty_recent", "episode"),
    )

    assert await bridge.build_llm_context_prefix() == ("", None)
    assert bridge.dynamic_context_projection_stats()["delivered_bytes"] == 0


def test_realtime_transport_projection_is_bounded_and_traceable() -> None:
    content = (
        "presence\n"
        + "\n".join(f"- assertion-{index}: {'世界' * 40}" for index in range(2_000))
        + "\nlatest-change"
    )

    projected, stats = _compact_context_lines(content, 4096)

    assert len(projected.encode("utf-8")) <= 4096
    assert projected.startswith("presence")
    assert projected.endswith("latest-change")
    assert stats["compacted"] is True
    assert stats["algorithm"] == "head-tail-lines-v1"
    assert stats["original_bytes"] > stats["delivered_bytes"]
    assert stats["delivered_bytes"] <= stats["max_bytes"] == 4096
    assert stats["omitted_lines"] > 0
    assert stats["projection_sha256"] == hashlib.sha256(
        projected.encode("utf-8")
    ).hexdigest()


def test_single_line_and_episode_projections_never_break_utf8_budget() -> None:
    source = '{"payload":"' + ("语音🌸\\\"" * 3_000) + '"}'

    projected, stats = _compact_context_lines(source, 2048)

    assert len(projected.encode("utf-8")) <= 2048
    assert projected.startswith(source[:10])
    assert projected.endswith(source[-10:])
    assert stats["compacted"] is True

    transcript = [{"role": "assistant", "text": source}]
    continuation, continuation_stats = _project_episode_transcript(
        transcript,
        2048,
    )
    assert len(continuation.encode("utf-8")) <= 2048
    records = [json.loads(line) for line in continuation.splitlines()]
    assert records[-1]["role"] == "assistant"
    assert records[-1]["text_suffix"]
    assert continuation_stats["source_turns"] == 1
    assert continuation_stats["delivered_turns"] == 1


@pytest.mark.asyncio
async def test_voice_prompt_projects_oversized_layers_without_truncating_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = VoiceLiveConfig()
    config.session.subject_context_max_bytes = 8 * 1024
    config.session.episode_context_max_bytes = 2 * 1024
    config.session.voice_instructions_max_bytes = 2 * 1024
    config.session.startup_context_max_bytes = 16 * 1024
    config.session.tool_result_context_max_bytes = 2 * 1024
    config.full_duplex.instructions = "语音覆盖层" * 5_000
    store = VoiceEpisodeStore(tmp_path, "voice_bounded", "episode")
    for index in range(80):
        await store.append_async(
            "transcript.final",
            {"role": "user" if index % 2 == 0 else "assistant", "text": "历史" * 300},
        )
    service = FakeLifeService(
        _subject_snapshot(max_bytes=config.session.subject_context_max_bytes)
    )
    monkeypatch.setattr(
        "plugins.voice_live.context_bridge.get_running_life_service",
        lambda: service,
    )
    bridge = ContextBridge(config, FakeConsciousness(), store)

    bundle = await bridge.build_system_prompt()

    assert len(bundle.text.encode("utf-8")) <= config.session.startup_context_max_bytes
    assert bundle.layers["episode_continuation"]["compacted"] is True
    assert bundle.layers["voice_interaction_overlay"]["compacted"] is True
    assert len(store.transcript()) == 80
    tool_result, tool_stats = bridge.project_tool_result({"blob": "工具" * 10_000})
    assert len(tool_result.encode("utf-8")) <= 2 * 1024
    assert tool_stats["original_bytes"] > tool_stats["delivered_bytes"]
    assert tool_stats["retention"] == "provider_response_ttl"


@pytest.mark.asyncio
async def test_voice_episode_reconnect_reuses_exact_subject_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = VoiceLiveConfig()
    snapshot = _subject_snapshot(max_bytes=config.session.subject_context_max_bytes)
    service = FakeLifeService(snapshot)
    monkeypatch.setattr(
        "plugins.voice_live.context_bridge.get_running_life_service",
        lambda: service,
    )
    store = VoiceEpisodeStore(tmp_path, "voice_resume", "episode")

    first = await ContextBridge(config, FakeConsciousness(), store).build_system_prompt()
    resumed_bundles = [
        await ContextBridge(config, FakeConsciousness(), store).build_system_prompt()
        for _ in range(12)
    ]

    assert all(bundle.text == first.text for bundle in resumed_bundles)
    assert service.projection_calls[-1]["source_digest"] == snapshot["source_digest"]
    assert service.projection_calls[-1]["projection_version"] == 1
    assert any(
        record.event == "subject_context.resumed" for record in store.read_all()
    )

    service.snapshot = _subject_snapshot(
        max_bytes=config.session.subject_context_max_bytes,
        suffix="已变化",
    )
    with pytest.raises(RuntimeError, match="重连"):
        await ContextBridge(config, FakeConsciousness(), store).build_system_prompt()


@pytest.mark.asyncio
async def test_voice_rejects_subject_manifest_that_contains_private_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = VoiceLiveConfig()
    snapshot = _subject_snapshot(max_bytes=config.session.subject_context_max_bytes)
    snapshot["sources"][0]["text"] = "不得进入审计的主体正文"
    service = FakeLifeService(snapshot)
    monkeypatch.setattr(
        "plugins.voice_live.context_bridge.get_running_life_service",
        lambda: service,
    )
    store = VoiceEpisodeStore(tmp_path, "voice_private_manifest", "episode")

    with pytest.raises(RuntimeError, match="禁止携带私密正文"):
        await ContextBridge(config, FakeConsciousness(), store).build_system_prompt()

    assert not any(
        record.event.startswith("subject_context.") for record in store.read_all()
    )


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
        "plugins.voice_live.context_bridge.get_running_life_service", lambda: None
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


def test_new_voice_live_install_registers_components_only_when_enabled() -> None:
    config = VoiceLiveConfig()
    assert config.plugin.enabled is False
    assert VoiceLivePlugin(config).get_components() == []

    enabled_config = VoiceLiveConfig(plugin={"enabled": True})
    plugin = VoiceLivePlugin(enabled_config)
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
