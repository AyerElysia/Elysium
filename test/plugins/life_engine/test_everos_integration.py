"""EverOS bridge tests for life_engine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.chatter import LifeChatter
from plugins.life_engine.core.everos import (
    build_everos_add_payload,
    build_everos_search_payload,
    format_everos_recall_block,
    recall_everos_for_chatter,
    sanitize_everos_id,
    sync_message_to_everos,
)
from plugins.life_engine.service import LifeEngineService
from src.core.models.message import Message, MessageType


@dataclass
class _DummyPlugin:
    config: object


def _make_service(tmp_path: Path) -> LifeEngineService:
    config = LifeEngineConfig()
    config.settings.enabled = True
    config.settings.workspace_path = str(tmp_path)
    return LifeEngineService(_DummyPlugin(config=config))


def test_everos_config_defaults_to_disabled() -> None:
    config = LifeEngineConfig()

    assert config.everos.enabled is False
    assert config.everos.base_url == "http://127.0.0.1:8000"
    assert config.everos.app_id == "neo_mofox"
    assert config.everos.timeout_seconds == 3.0
    assert config.everos.write_timeout_seconds == 15.0
    assert config.everos.recall_timeout_seconds == 2.0


def test_everos_and_chatter_config_fields_are_visible() -> None:
    visible = LifeEngineConfig.__config_schema_visible_fields__

    assert visible["everos"] == {
        "enabled",
        "base_url",
        "app_id",
        "project_id",
        "sync_messages",
        "sync_sent_messages",
        "recall_to_chatter",
        "search_method",
        "top_k",
        "include_profile",
        "timeout_seconds",
        "write_timeout_seconds",
        "recall_timeout_seconds",
        "flush_after_add",
        "max_recall_chars",
    }
    assert {
        "recent_history_tail_messages",
        "enable_sub_agent",
        "sub_agent_task_name",
        "sub_agent_allow_mcp",
        "sub_agent_default_max_rounds",
        "enable_mcp",
    } <= visible["chatter"]


@pytest.mark.parametrize(
    "value",
    ["neo", "qq_user-123", "a.b+c@example"],
)
def test_everos_id_sanitization_preserves_safe_ascii(value: str) -> None:
    assert sanitize_everos_id(value, fallback="neo_user") == value


def test_everos_id_sanitization_is_path_safe() -> None:
    assert (
        sanitize_everos_id("qq/group/中文 user", fallback="neo_user")
        == "qq_group_user-b40b96a00b37"
    )
    assert sanitize_everos_id("..", fallback="neo_user") == "neo_user"
    assert sanitize_everos_id("", fallback="neo_user") == "neo_user"


def test_everos_id_sanitization_avoids_unsafe_and_unicode_collisions() -> None:
    path_ids = {
        sanitize_everos_id("a/b"),
        sanitize_everos_id("a b"),
    }
    unicode_ids = {
        sanitize_everos_id("用户/a"),
        sanitize_everos_id("访客/a"),
    }

    assert len(path_ids) == 2
    assert len(unicode_ids) == 2
    assert all(identifier.startswith("a_") for identifier in path_ids)
    assert all(identifier.startswith("a-") for identifier in unicode_ids)


def test_everos_id_sanitization_hashes_truncated_values_within_limit() -> None:
    values = [f"account-{'x' * 64}-{suffix}" for suffix in ("one", "two")]
    identifiers = [sanitize_everos_id(value, max_length=24) for value in values]

    assert identifiers[0] != identifiers[1]
    assert identifiers[0] == sanitize_everos_id(values[0], max_length=24)
    assert all(len(identifier) == 24 for identifier in identifiers)
    assert all(identifier.isascii() for identifier in identifiers)
    assert all(set(identifier) <= set("abcdefghijklmnopqrstuvwxyz0123456789-") for identifier in identifiers)


def test_everos_add_payload_uses_plain_text_and_safe_ids() -> None:
    config = LifeEngineConfig()
    config.everos.enabled = True
    message = Message(
        message_id="m1",
        time=1_700_000_000.123,
        content={"raw": "ignored"},
        processed_plain_text="你好 Neo",
        message_type=MessageType.TEXT,
        sender_id="qq/user 123",
        sender_name="Alice",
        sender_role="member",
        platform="qq",
        chat_type="private",
        stream_id="qq/private/user 123",
    )

    payload = build_everos_add_payload(config, message, direction="received")

    assert payload is not None
    assert payload["session_id"] == "qq@qq_private_user_123-1043bb05e362"
    assert payload["app_id"] == "neo_mofox"
    assert payload["messages"][0]["sender_id"] == "qq@qq_user_123-6a5ddfd5661b"
    assert payload["messages"][0]["sender_name"] == "Alice"
    assert payload["messages"][0]["role"] == "user"
    assert payload["messages"][0]["timestamp"] == 1_700_000_000_123
    assert payload["messages"][0]["content"] == "你好 Neo"

    search_payload = build_everos_search_payload(
        config,
        query="你好",
        user_id=message.sender_id,
        platform=message.platform,
    )
    assert search_payload is not None
    assert search_payload["user_id"] == payload["messages"][0]["sender_id"]


def test_everos_add_payload_omits_raw_media_data() -> None:
    config = LifeEngineConfig()
    config.everos.enabled = True
    blob = "A" * 256
    message = Message(
        content={
            "type": "image",
            "caption": f"猫猫 data:image/png;base64,{blob}",
            "media": [{"url": "https://example.test/cat.png", "data": blob}],
            "raw": {"base64": blob},
        },
        processed_plain_text={"raw": blob},
        message_type=MessageType.IMAGE,
        sender_id="u1",
        platform="qq",
        stream_id="s1",
    )

    payload = build_everos_add_payload(config, message)

    assert payload is not None
    content = payload["messages"][0]["content"]
    assert "猫猫" in content
    assert "[媒体数据已省略]" in content
    assert blob not in content
    assert "https://example.test" not in content

    media_only = Message(
        content={"media": [{"data": blob}], "raw": blob},
        processed_plain_text={"base64": blob},
        message_type=MessageType.IMAGE,
        sender_id="u1",
        platform="qq",
        stream_id="s1",
    )
    media_payload = build_everos_add_payload(config, media_only)
    assert media_payload is not None
    assert media_payload["messages"][0]["content"] == "[图片消息]"


def test_everos_invalid_timestamps_fall_back_to_now(monkeypatch) -> None:
    config = LifeEngineConfig()
    config.everos.enabled = True
    monkeypatch.setattr("plugins.life_engine.core.everos.time.time", lambda: 1_800_000_000.25)

    for invalid_time in (float("nan"), float("inf"), "invalid", 0, -1):
        message = Message(
            time=1,
            content="hello",
            processed_plain_text="hello",
            sender_id="u1",
            platform="qq",
            stream_id="s1",
        )
        message.time = invalid_time

        payload = build_everos_add_payload(config, message)

        assert payload is not None
        assert payload["messages"][0]["timestamp"] == 1_800_000_000_250


def test_everos_search_payload_can_be_disabled() -> None:
    config = LifeEngineConfig()

    assert build_everos_search_payload(config, query="hello", user_id="u1") is None

    config.everos.enabled = True
    payload = build_everos_search_payload(
        config,
        query="hello",
        user_id="u/1",
        platform="qq",
    )

    assert payload is not None
    assert payload["user_id"] == "qq@u_1-4f435f2f803c"
    assert payload["method"] == "hybrid"
    assert payload["include_profile"] is True


def test_everos_recall_formatter_compacts_search_response() -> None:
    block = format_everos_recall_block(
        {
            "data": {
                "profiles": [
                    {"profile_data": {"likes": ["Markdown memory"], "tone": "direct"}}
                ],
                "episodes": [
                    {
                        "summary": "用户之前说核心记忆都是 md 文件。",
                        "episode": "讨论 EverOS 和 Neo 记忆系统能否结合。",
                        "atomic_facts": [{"content": "用户愿意删除 booku_memory 插件。"}],
                    }
                ],
            }
        },
        max_chars=1000,
    )

    assert "EverOS 长期记忆召回" in block
    assert "用户画像" in block
    assert "核心记忆都是 md 文件" in block
    assert "booku_memory" in block


@pytest.mark.asyncio
async def test_life_chatter_injects_everos_recall_when_enabled(monkeypatch) -> None:
    async def fake_recall(
        _cfg,
        *,
        query: str,
        user_id: str,
        platform: str,
    ) -> str:
        assert "新消息" in query
        assert user_id == "u1"
        assert platform == "qq"
        return "### EverOS 长期记忆召回\n- 过往片段：记得用户偏好 md 记忆"

    monkeypatch.setattr(
        "plugins.life_engine.core.chatter.recall_everos_for_chatter",
        fake_recall,
    )

    config = LifeEngineConfig()
    config.everos.enabled = True
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)
    stream = SimpleNamespace(stream_id="stream-1", platform="qq")
    message = Message(
        message_id="m1",
        content="新消息",
        processed_plain_text="新消息",
        sender_id="u1",
        sender_name="Alice",
        sender_role="member",
        platform="qq",
        stream_id="stream-1",
    )

    dynamic, high_water = await chatter._build_dynamic_context_text(
        stream,
        service=None,
        runtime_context_text="RUNTIME",
        everos_query_text="新消息",
        everos_unread_messages=[message],
    )

    assert high_water == 0
    assert "RUNTIME" in dynamic
    assert "EverOS 长期记忆召回" in dynamic
    assert "记得用户偏好 md 记忆" in dynamic


@pytest.mark.asyncio
async def test_live_bridge_recall_uses_formatted_sender_id(monkeypatch) -> None:
    async def fake_recall(
        _cfg,
        *,
        query: str,
        user_id: str,
        platform: str,
    ) -> str:
        assert "观众问题" in query
        assert user_id == "live_user_42"
        assert platform == "live"
        return "### EverOS 长期记忆召回\n- 过往片段：直播用户记忆"

    monkeypatch.setattr(
        "plugins.life_engine.core.chatter.recall_everos_for_chatter",
        fake_recall,
    )

    config = LifeEngineConfig()
    config.everos.enabled = True
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)
    stream = SimpleNamespace(
        stream_id="live-stream-1",
        stream_name="直播间",
        platform="live",
    )

    bundle = await chatter.build_live_bridge_prompt(
        stream,
        service=None,
        unread_lines="【02:40】[live_user_42] 观众A [m1]： 观众问题",
        include_history_in_prompt=False,
    )

    assert "直播用户记忆" in bundle["suffix_prompt"]


@pytest.mark.asyncio
async def test_record_message_schedules_only_confirmed_sent_everos_sync(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scheduled: list[dict[str, object]] = []
    done_callbacks = []

    class FakeTask:
        def add_done_callback(self, callback) -> None:
            done_callbacks.append(callback)

    class FakeTaskManager:
        def create_task(self, coro, **kwargs):
            scheduled.append(kwargs)
            coro.close()
            return SimpleNamespace(task=FakeTask(), task_id="everos-task-1")

    monkeypatch.setattr(
        "plugins.life_engine.service.core.get_task_manager",
        lambda: FakeTaskManager(),
    )

    service = _make_service(tmp_path)
    service.plugin.config.everos.enabled = True
    service.plugin.config.everos.write_timeout_seconds = 12.0
    message = Message(
        message_id="m1",
        time=1_700_000_000,
        content="hello",
        processed_plain_text="hello",
        sender_id="u1",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        stream_id="s1",
    )

    await service.record_message(message, direction="sent")
    assert scheduled == []

    await service.record_message(message, direction="sent", sent_confirmed=True)

    assert len(scheduled) == 1
    assert scheduled[0]["name"] == "life_everos_sync_2"
    assert scheduled[0]["daemon"] is True
    assert scheduled[0]["timeout"] == 14.0
    assert service._everos_sync_task_ids == {"everos-task-1"}
    assert len(done_callbacks) == 1

    done_callbacks[0](None)
    assert service._everos_sync_task_ids == set()


@pytest.mark.asyncio
async def test_everos_uses_separate_write_and_recall_timeouts(monkeypatch) -> None:
    calls: list[tuple[str, float]] = []

    async def fake_to_thread(fn, url, payload, timeout):
        _ = (fn, payload)
        calls.append((url, timeout))
        if url.endswith("/api/v1/memory/search"):
            return {"data": {"episodes": []}}
        return {"data": {"status": "accumulated"}}

    monkeypatch.setattr("plugins.life_engine.core.everos.asyncio.to_thread", fake_to_thread)

    config = LifeEngineConfig()
    config.everos.enabled = True
    config.everos.base_url = "http://everos.local"
    config.everos.write_timeout_seconds = 12.0
    config.everos.recall_timeout_seconds = 1.5
    message = Message(
        message_id="m1",
        content="hello",
        processed_plain_text="hello",
        sender_id="u1",
        sender_name="Alice",
        platform="qq",
        chat_type="private",
        stream_id="s1",
    )

    await sync_message_to_everos(config, message)
    await recall_everos_for_chatter(config, query="hello", user_id="u1")

    assert calls == [
        ("http://everos.local/api/v1/memory/add", 12.0),
        ("http://everos.local/api/v1/memory/search", 1.5),
    ]
