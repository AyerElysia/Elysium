"""life_engine 对话提示词与叙事测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import plugins.life_engine.core.chatter as chatter_module

from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.context_assembly import LifeChatterContextAssembler
from plugins.life_engine.core.chatter import (
    LifeChatter,
    _GLOBAL_RUNTIME_BUSY_RETRY_SECONDS,
    _Phase,
    _WorkflowRuntime,
)
from plugins.life_engine.constants import LIFE_CHATTER_GLOBAL_CURSOR_KEY
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent
from plugins.life_engine.service.perception_gateway import (
    PerceptionDeliveryReceipt,
    PerceptionDeliveryUnverified,
)
from plugins.life_engine.tools.exec_tools import LifeEngineBashTool
from plugins.life_engine.tools.file_tools import LifeEngineRunAgentTool, LifeEngineWakeDFCTool
from src.core.components.base.chatter import BaseChatter, Failure, Success, Wait
from src.core.models.media import MediaAttachment
from src.core.models.message import Message, MessageType
from src.core.utils.llm_tool_call import ToolCallExecutionResult
from src.kernel.llm import Image, LLMContextManager, LLMPayload, ROLE, Text, ToolResult
import pytest


_TEST_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


async def _skip_snapshot_save(_response: object) -> None:
    """替换 ``_save_rolling_context_snapshot`` 的异步空实现。

    快照落盘会 mkdir/write_text/os.replace，是真实磁盘 I/O，因此生产实现是
    协程；桩必须同样是协程，否则调用点的 ``await`` 会拿到 ``None``。

    Args:
        _response: 被忽略的 LLM 响应对象。
    """
    return None


def _exact_pending_perception_receipt(
    service: LifeEngineService,
    stream_id: str,
    *,
    unified_chatter_context: bool = False,
) -> PerceptionDeliveryReceipt:
    delivery = service.get_pending_chatter_runtime_delivery(
        stream_id,
        unified_chatter_context=unified_chatter_context,
    )
    assert delivery is not None
    prepared = delivery.prepared_perception
    return PerceptionDeliveryReceipt(
        delivery_id=prepared.delivery_id,
        projection_sha256=prepared.projection_sha256,
        delivered_bytes=prepared.delivered_bytes,
        exact=True,
        transport_request_id="test-request",
    )


def test_life_chatter_system_prompt_includes_memory_and_chatter_tools_not_heartbeat_tool(tmp_path) -> None:
    """聊天态应共享 SOUL/USER/MEMORY/TOOLS，并保留核心工具说明。"""
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text(
        "\n".join(
            [
                "# 值得记住的事",
                "",
                "这里是一大段给编辑者看的说明，不该原样注入。",
                "",
                "### Durable（持久）",
                "- MEMORY_DURABLE",
                "",
                "### Active（活跃）",
                "- MEMORY_ACTIVE",
                "",
                "### Fading（待审视）",
                "- MEMORY_FADING",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "TOOL.md").write_text("TOOL_CONTENT", encoding="utf-8")
    (tmp_path / "TOOLS.md").write_text("CHATTER_TOOLS_CONTENT", encoding="utf-8")

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)
    prompt = chatter._build_chat_system_prompt(service=None)

    assert "SOUL_CONTENT" in prompt
    assert "USER_CONTENT" in prompt
    assert "MEMORY_DURABLE" in prompt
    assert "MEMORY_ACTIVE" in prompt
    assert "MEMORY_FADING" not in prompt
    assert "给编辑者看的说明" not in prompt
    assert "TOOL_CONTENT" not in prompt
    assert "CHATTER_TOOLS_CONTENT" in prompt
    assert "assistant 纯文本 **不会被发送给用户**" in prompt
    assert "自己的内心独白" in prompt
    assert "action-life_pass_and_wait" in prompt
    assert "life_send_text" in prompt
    assert "reason" in prompt
    assert "action-think" not in LifeChatter._build_primary_tool_guide()
    assert "需要的轻量思考应在当前模型决策内完成" in prompt
    assert "普通回复直接调用 `life_send_text`" in prompt
    assert "一次能完成的事拆成两次模型调用" in prompt
    assert "多个互不依赖的工具调用" in prompt
    assert "安全、无副作用" in prompt


@pytest.mark.asyncio
async def test_life_chatter_router_uses_only_derived_projection() -> None:
    """Router receives the derived read model, never full authority/tool files."""

    class _ProjectionService:
        async def get_router_context_projection_prompt(self) -> str:
            return "DERIVED_ROUTER_PROJECTION"

    chatter = LifeChatter.__new__(LifeChatter)
    prompt = await chatter._build_chat_router_prefix_prompt(
        service=_ProjectionService(),
    )

    assert prompt == "DERIVED_ROUTER_PROJECTION"
    assert "SOUL_CONTENT" not in prompt
    assert "USER_CONTENT" not in prompt
    assert "MEMORY_DURABLE" not in prompt
    assert "CHATTER_TOOLS_CONTENT" not in prompt


def test_life_chatter_persistent_user_prompt_excludes_dynamic_context() -> None:
    """持久 USER prompt 不应写入 inner_state/recent_context 等动态快照。"""
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(stream_name="Test", stream_id="stream-1")

    prompt = chatter._build_chat_user_prompt(
        chat_stream,
        unread_lines="新消息",
        history_text="历史消息",
    )

    assert "<chat_history>" in prompt
    assert "<new_messages>" in prompt
    assert "<inner_state>" not in prompt
    assert "<recent_context>" not in prompt
    assert "<runtime_assistant_context>" not in prompt


def test_life_chatter_context_compression_hook_preserves_dropped_history() -> None:
    manager = LLMContextManager()
    request = SimpleNamespace(context_manager=manager)

    LifeChatter._install_context_compression_hook(request)

    payloads = [
        LLMPayload(ROLE.SYSTEM, Text("system")),
        LLMPayload(ROLE.USER, Text("旧用户消息")),
        LLMPayload(ROLE.ASSISTANT, Text("旧回复")),
        LLMPayload(ROLE.USER, Text("新用户消息")),
        LLMPayload(ROLE.ASSISTANT, Text("新回复")),
    ]

    trimmed = manager.maybe_trim(
        payloads,
        max_token_budget=4,
        token_counter=lambda items: len(items),
    )

    assert len(trimmed) == 4
    assert trimmed[0].role == ROLE.SYSTEM
    assert trimmed[1].role == ROLE.USER
    compressed = trimmed[1].content[0].text
    assert "<compressed_life_chatter_context>" in compressed
    assert "旧用户消息" in compressed
    assert "旧回复" in compressed
    assert trimmed[2].content[0].text == "新用户消息"


def test_life_chatter_context_compression_hook_uses_configured_limits() -> None:
    manager = LLMContextManager()
    request = SimpleNamespace(context_manager=manager)
    chatter_config = SimpleNamespace(
        context_compression_max_groups=1,
        context_compression_max_part_chars=16,
    )

    LifeChatter._install_context_compression_hook(
        request, chatter_config=chatter_config
    )
    compressed_payloads = manager.compression_hook(
        [
            [LLMPayload(ROLE.USER, Text("第一组用户消息" + "x" * 100))],
            [LLMPayload(ROLE.USER, Text("第二组用户消息" + "a" * 100))],
        ],
        [],
    )

    compressed = compressed_payloads[0].content[0].text
    assert "更早的 1 组上下文已进一步省略" in compressed
    assert "第一组用户消息" not in compressed
    assert "第二组用户消息" in compressed
    assert "..." in compressed


def test_life_chatter_rolling_context_snapshot_is_compacted_with_summary() -> None:
    payloads = [
        LLMPayload(ROLE.USER, Text(f"旧用户消息-{index}" + "x" * 20_000))
        if index % 2 == 0
        else LLMPayload(ROLE.ASSISTANT, Text(f"旧回复-{index}" + "y" * 20_000))
        for index in range(30)
    ]
    payloads.extend(
        [
            LLMPayload(ROLE.USER, Text("最新用户消息")),
            LLMPayload(ROLE.ASSISTANT, Text("最新回复")),
        ]
    )

    compacted, before_chars, after_chars = LifeChatter._compact_rolling_context_payloads(
        payloads,
    )

    assert after_chars < before_chars
    assert after_chars <= 320_000
    assert compacted[0].role == ROLE.USER
    assert "<compressed_life_chatter_context>" in compacted[0].content[0].text
    assert "旧用户消息" in compacted[0].content[0].text
    assert any(
        part.text == "最新用户消息"
        for payload in compacted
        for part in payload.content
        if isinstance(part, Text)
    )


def test_life_chatter_single_huge_snapshot_payload_has_hard_cap() -> None:
    payloads = [LLMPayload(ROLE.USER, Text("超大消息" + "x" * 400_000))]

    compacted, before_chars, after_chars = LifeChatter._compact_rolling_context_payloads(
        payloads,
    )

    assert before_chars > 320_000
    assert after_chars <= 320_000
    assert LifeChatter._estimate_payload_chars(compacted) == after_chars
    assert "<compressed_life_chatter_context>" in compacted[0].content[0].text


def test_life_chatter_snapshot_compaction_drops_large_binary_and_tool_payloads() -> None:
    payloads = [
        LLMPayload(
            ROLE.USER,
            [
                Text("请看附件"),
                Image(
                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                    "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
                ),
            ],
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value={"raw": "z" * 400_000}, call_id="call-1", name="dump"),
        ),
        LLMPayload(ROLE.USER, Text("最新问题")),
        LLMPayload(ROLE.ASSISTANT, Text("最新回答")),
    ]

    compacted, before_chars, after_chars = LifeChatter._compact_rolling_context_payloads(
        payloads,
        char_budget=2_000,
    )

    assert before_chars > 2_000
    assert after_chars <= 2_000
    assert LifeChatter._estimate_payload_chars(compacted) == after_chars
    serialized = str(LifeChatter._snapshot_data_for_payloads(compacted))
    assert "a" * 1_000 not in serialized
    assert "z" * 1_000 not in serialized
    assert "[图片]" in serialized
    assert "[工具结果]" in serialized


def test_life_chatter_snapshot_persists_only_media_descriptor() -> None:
    encoded = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    image = Image(encoded, source_message_id="message-1")

    snapshot = LifeChatter._snapshot_data_for_payloads(
        [LLMPayload(ROLE.USER, [Text("请看图片"), image])]
    )

    media_part = snapshot["payloads"][0]["content"][1]
    assert media_part == {
        "type": "media_descriptor",
        "descriptor": {
            "kind": "image",
            "mime_type": "image/png",
            "size_bytes": image.size_bytes,
            "sha256": image.sha256,
            "source_message_id": "message-1",
        },
    }
    assert encoded not in str(snapshot)
    assert "value" not in media_part


def test_life_chatter_snapshot_media_restores_as_text_only() -> None:
    descriptor_payload = {
        "role": "user",
        "content": [
            {
                "type": "media_descriptor",
                "descriptor": {
                    "kind": "image",
                    "mime_type": "image/png",
                    "size_bytes": 68,
                    "sha256": "a" * 64,
                    "source_message_id": "message-1",
                },
            }
        ],
    }
    legacy_payload = {
        "role": "user",
        "content": [
            {
                "type": "image",
                "value": "legacy-image-body",
                "mime_type": "image/png",
            }
        ],
    }

    restored = LifeChatter._deserialize_payload(descriptor_payload)
    restored_legacy = LifeChatter._deserialize_payload(legacy_payload)

    assert restored is not None
    assert restored_legacy is not None
    assert all(isinstance(part, Text) for part in restored.content)
    assert all(isinstance(part, Text) for part in restored_legacy.content)
    assert "原始媒体数据已释放" in restored.content[0].text
    assert "message-1" in restored.content[0].text
    assert "legacy-image-body" not in restored_legacy.content[0].text


def test_life_chatter_snapshot_compaction_handles_budget_below_summary_envelope() -> None:
    payloads = [LLMPayload(ROLE.USER, Text("x" * 10_000))]
    empty_snapshot_chars = LifeChatter._estimate_payload_chars([])

    compacted, before_chars, after_chars = LifeChatter._compact_rolling_context_payloads(
        payloads,
        char_budget=empty_snapshot_chars,
    )

    assert before_chars > empty_snapshot_chars
    assert compacted == []
    assert after_chars == empty_snapshot_chars


@pytest.mark.parametrize("failure_stage", ["mkdir", "write", "replace"])
async def test_life_chatter_snapshot_save_failure_does_not_mutate_runtime(
    tmp_path, monkeypatch, failure_stage
) -> None:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    config.chatter.context_compaction_trigger_chars = 1_000
    config.chatter.context_compaction_target_chars = 500
    config.chatter.context_compaction_min_recent_groups = 1
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)
    payloads = [
        LLMPayload(ROLE.USER, Text(f"旧消息-{index}" + "x" * 2_000))
        if index % 2 == 0
        else LLMPayload(ROLE.ASSISTANT, Text(f"旧回复-{index}" + "y" * 2_000))
        for index in range(8)
    ]
    response = SimpleNamespace(payloads=payloads)
    original_list = response.payloads
    original_payloads = list(original_list)
    original_contents = [payload.content for payload in original_payloads]

    compactable_response = SimpleNamespace(payloads=list(payloads))
    result = chatter._maybe_compact_runtime_context(compactable_response)
    assert result.triggered
    assert compactable_response.payloads is not payloads
    assert len(compactable_response.payloads) < len(payloads)

    if failure_stage == "mkdir":
        monkeypatch.setattr(Path, "mkdir", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("mkdir")))
    elif failure_stage == "write":
        monkeypatch.setattr(Path, "write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write")))
    else:
        monkeypatch.setattr(chatter_module.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace")))

    await chatter._save_rolling_context_snapshot(response)

    assert response.payloads is original_list
    assert response.payloads == original_payloads
    assert all(actual is expected for actual, expected in zip(response.payloads, original_payloads, strict=True))
    assert all(payload.content is content for payload, content in zip(response.payloads, original_contents, strict=True))


async def test_life_chatter_global_runtime_is_reused(monkeypatch) -> None:
    LifeChatter.reset_global_runtime()
    created_requests: list[SimpleNamespace] = []

    def fake_create_request(self, *_args, **_kwargs):
        request = SimpleNamespace(payloads=[])
        request.add_payload = lambda payload: request.payloads.append(payload)
        created_requests.append(request)
        return request

    async def fake_inject_usables(self, request):
        return {"request_id": id(request)}

    monkeypatch.setattr(LifeChatter, "create_request", fake_create_request)
    monkeypatch.setattr(LifeChatter, "inject_usables", fake_inject_usables)
    monkeypatch.setattr(LifeChatter, "_build_chat_system_prompt", lambda self, *a, **kw: "test soul")

    first = LifeChatter.__new__(LifeChatter)
    first.plugin = SimpleNamespace(config=None)
    first.stream_id = "stream-a"
    second = LifeChatter.__new__(LifeChatter)
    second.plugin = SimpleNamespace(config=None)
    second.stream_id = "stream-b"

    stream_a = SimpleNamespace(stream_id="stream-a")
    stream_b = SimpleNamespace(stream_id="stream-b")

    rt_a, usable_a = await first._get_or_create_global_runtime(None, stream_a)
    rt_b, usable_b = await second._get_or_create_global_runtime(None, stream_b)

    assert rt_a is rt_b
    assert usable_a is usable_b
    assert len(created_requests) == 1

    LifeChatter.reset_global_runtime()


def _life_chatter_test_image_message(*, descriptor_only: bool = False) -> Message:
    attachment = MediaAttachment.from_legacy(
        {"type": "image", "data": _TEST_PNG_B64},
        source_message_id="image-message",
    )
    if descriptor_only:
        attachment = MediaAttachment.from_descriptor(attachment.to_descriptor())
    return Message(
        message_id="image-message",
        content="[图片]",
        processed_plain_text="[图片]",
        message_type=MessageType.IMAGE,
        sender_role="other",
        stream_id="stream-a",
        attachments=[attachment],
    )


def _life_chatter_for_config(config: object | None) -> LifeChatter:
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)
    chatter.stream_id = "stream-a"
    return chatter


def test_life_chatter_observable_unread_media_respects_materialization_and_config() -> None:
    config = LifeEngineConfig()
    chatter = _life_chatter_for_config(config)

    materialized, _ = chatter._extract_unread_media(
        [_life_chatter_test_image_message()]
    )
    descriptor_only, _ = chatter._extract_unread_media(
        [_life_chatter_test_image_message(descriptor_only=True)]
    )
    empty_body, _ = chatter._extract_unread_media(
        [
            Message(
                message_id="empty-image",
                content={"media": [{"type": "image", "data": ""}]},
                processed_plain_text="[图片]",
                message_type=MessageType.IMAGE,
                sender_role="other",
            )
        ]
    )

    assert chatter._has_observable_media(materialized) is True
    assert chatter._has_observable_media(descriptor_only) is False
    assert chatter._has_observable_media(empty_body) is False

    config.multimodal.native_image = False
    disabled, _ = chatter._extract_unread_media(
        [_life_chatter_test_image_message()]
    )
    assert chatter._has_observable_media(disabled) is False

    config.multimodal.native_image = True
    config.multimodal.max_images_per_payload = 0
    over_budget, _ = chatter._extract_unread_media(
        [_life_chatter_test_image_message()]
    )
    assert chatter._has_observable_media(over_budget) is False

    config.multimodal.max_images_per_payload = 4
    compatible, _ = chatter._extract_unread_media(
        [
            Message(
                message_id="legacy-image",
                content={"media": [{"type": "image", "data": _TEST_PNG_B64}]},
                processed_plain_text="[图片]",
                message_type=MessageType.IMAGE,
                sender_role="other",
            )
        ]
    )
    assert chatter._has_observable_media(compatible) is True


class _RouterBranchRequest:
    def __init__(self, flushed: list[Message], *, must_not_send: bool = False) -> None:
        self.payloads: list[LLMPayload] = []
        self.call_list = [
            SimpleNamespace(
                id="pass-1",
                name="action-life_pass_and_wait",
                args={},
            )
        ]
        self.message = ""
        self.flushed = flushed
        self.must_not_send = must_not_send
        self.send_calls = 0
        self.saw_native_image = False

    def add_payload(self, payload: LLMPayload) -> None:
        self.payloads.append(payload)

    async def send(self, *, stream: bool = False):
        assert stream is False
        if self.must_not_send:
            raise AssertionError("router=false 的非媒体消息不得进入主 request")
        assert self.flushed == [], "unread 不得在主模型发送前被路由阶段 flush"
        self.send_calls += 1
        self.saw_native_image = any(
            isinstance(part, Image)
            for payload in self.payloads
            for part in payload.content
        )
        return self

    def __await__(self):
        async def done():
            return self

        return done().__await__()


async def _drive_router_false_case(
    monkeypatch,
    *,
    unread: Message,
    config: LifeEngineConfig,
    must_not_send: bool,
) -> tuple[Wait | Success | Failure, _RouterBranchRequest, list[Message]]:
    LifeChatter.reset_global_runtime()
    flushed: list[Message] = []
    request = _RouterBranchRequest(flushed, must_not_send=must_not_send)
    rt = _WorkflowRuntime(
        response=request,
        phase=_Phase.WAIT_USER,
        history_merged=False,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}
    chatter = _life_chatter_for_config(config)
    chat_stream = SimpleNamespace(
        stream_id="stream-a",
        stream_name="Test",
        platform="test",
        chat_type="private",
        context=SimpleNamespace(history_messages=[]),
    )

    async def fetch_unreads():
        return [], [unread]

    async def router_false(*_args, **_kwargs):
        return {"reason": "test false", "should_respond": False}

    async def no_history(*_args, **_kwargs):
        return ""

    async def no_dynamic_context(*_args, **_kwargs):
        return "", 0

    async def flush_unreads(messages):
        flushed.extend(messages)

    async def immediate_model_turn(awaitable):
        return await awaitable

    monkeypatch.setattr(chatter, "fetch_unreads", fetch_unreads)
    monkeypatch.setattr(chatter, "_should_respond", router_false)
    monkeypatch.setattr(chatter, "_build_history_text_async", no_history)
    monkeypatch.setattr(chatter, "_build_dynamic_context_text", no_dynamic_context)
    monkeypatch.setattr(chatter, "flush_unreads", flush_unreads)
    monkeypatch.setattr(chatter, "_await_model_turn", immediate_model_turn)
    monkeypatch.setattr(chatter, "_maybe_compact_runtime_context", lambda _response: None)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)
    monkeypatch.setattr(
        "src.kernel.concurrency.get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda _stream_id: None),
    )

    result = await chatter._drive_global_runtime_until_yield(chat_stream, service=None)
    LifeChatter.reset_global_runtime()
    return result, request, flushed


async def test_life_chatter_router_false_with_materialized_image_reaches_main_request(
    monkeypatch,
) -> None:
    result, request, flushed = await _drive_router_false_case(
        monkeypatch,
        unread=_life_chatter_test_image_message(),
        config=LifeEngineConfig(),
        must_not_send=False,
    )

    assert isinstance(result, Wait)
    assert request.send_calls == 1
    assert request.saw_native_image is True
    assert flushed and flushed[0].message_id == "image-message"


@pytest.mark.parametrize("case", ["text", "descriptor", "disabled"])
async def test_life_chatter_router_false_without_observable_media_flushes_and_waits(
    monkeypatch,
    case: str,
) -> None:
    config = LifeEngineConfig()
    if case == "text":
        unread = Message(
            message_id="text-message",
            content="普通文本",
            processed_plain_text="普通文本",
            message_type=MessageType.TEXT,
            sender_role="other",
            stream_id="stream-a",
        )
    else:
        unread = _life_chatter_test_image_message(
            descriptor_only=case == "descriptor"
        )
        if case == "disabled":
            config.multimodal.native_image = False

    result, request, flushed = await _drive_router_false_case(
        monkeypatch,
        unread=unread,
        config=config,
        must_not_send=True,
    )

    assert isinstance(result, Wait)
    assert request.send_calls == 0
    assert request.payloads == []
    assert flushed == [unread]


def _model_entry(*modalities: str) -> dict[str, object]:
    return {"media_capabilities": {"modalities": list(modalities)}}


@pytest.mark.parametrize(
    ("configured_task", "configured_models", "multimodal_enabled", "expected_calls"),
    [
        ("vision-life", [_model_entry("text", "image")], True, ["vision-life"]),
        ("life", [_model_entry("text")], True, ["life", "expression"]),
        ("life", [_model_entry("text")], False, ["life"]),
        ("missing-models", [], True, ["missing-models", "expression"]),
        ("", [], True, ["expression"]),
    ],
)
def test_life_chatter_primary_task_selection_and_media_safe_fallback(
    monkeypatch,
    configured_task: str,
    configured_models: list[dict[str, object]],
    multimodal_enabled: bool,
    expected_calls: list[str],
) -> None:
    config = LifeEngineConfig()
    config.model.task_name = configured_task
    config.multimodal.enabled = multimodal_enabled
    chatter = _life_chatter_for_config(config)
    calls: list[str] = []

    def fake_create_request(_self, task, *, request_name):
        assert request_name == "life_chatter"
        calls.append(task)
        models = configured_models if task == configured_task else [
            _model_entry("text", "image")
        ]
        return SimpleNamespace(task=task, model_set=models)

    monkeypatch.setattr(LifeChatter, "create_request", fake_create_request)

    request = chatter._create_global_request()

    assert calls == expected_calls
    assert request.task == expected_calls[-1]


def test_life_chatter_primary_task_creation_error_falls_back_to_expression(monkeypatch) -> None:
    config = LifeEngineConfig()
    config.model.task_name = "broken-life"
    chatter = _life_chatter_for_config(config)
    calls: list[str] = []

    def fake_create_request(_self, task, *, request_name):
        assert request_name == "life_chatter"
        calls.append(task)
        if task == "broken-life":
            raise ValueError("task missing")
        return SimpleNamespace(task=task, model_set=[_model_entry("text", "image")])

    monkeypatch.setattr(LifeChatter, "create_request", fake_create_request)

    assert chatter._create_global_request().task == "expression"
    assert calls == ["broken-life", "expression"]


async def test_surface_private_message_skips_router_llm(monkeypatch) -> None:
    chatter = _life_chatter_for_config(LifeEngineConfig())
    unread = Message(
        message_id="surface-direct-1",
        content="爱莉爱莉",
        processed_plain_text="爱莉爱莉",
        sender_role="other",
        platform="neko.surface",
        stream_id="surface-stream",
    )
    chat_stream = SimpleNamespace(
        stream_id="surface-stream",
        platform="neko.surface",
        chat_type="private",
    )

    async def must_not_read_history(*_args, **_kwargs):
        raise AssertionError("Surface 实时私聊不应先读取路由历史")

    monkeypatch.delenv("NEKO_SURFACE_LOW_LATENCY", raising=False)
    monkeypatch.setattr(chatter, "_build_history_text_async", must_not_read_history)

    decision = await chatter._should_respond("主人: 爱莉爱莉", [unread], chat_stream)

    assert decision == {
        "reason": "N.E.K.O 实时私聊直接进入表达层",
        "should_respond": True,
        "force_reply": True,
    }


async def test_surface_dynamic_context_includes_realtime_guidance(monkeypatch) -> None:
    chatter = _life_chatter_for_config(LifeEngineConfig())
    monkeypatch.delenv("NEKO_SURFACE_LOW_LATENCY", raising=False)

    context_text, high_water = await chatter._build_dynamic_context_text(
        SimpleNamespace(platform="neko.surface"),
        service=None,
    )

    assert high_water == 0
    assert "N.E.K.O 实时私聊" in context_text
    assert "第一次模型决策里直接调用 `life_send_text`" in context_text
    assert "不要主动调用 `tts_voice_action`" in context_text


def test_surface_request_overrides_are_temporary(monkeypatch) -> None:
    class ThinkTool:
        @classmethod
        def get_signature(cls):
            return "life_engine:action:think"

    class SendTextTool:
        @classmethod
        def get_signature(cls):
            return "life_engine:action:life_send_text"

    class TtsTool:
        @classmethod
        def get_signature(cls):
            return "tts_voice_plugin:action:tts_voice_action"

    tool_payload = LLMPayload(ROLE.TOOL, [ThinkTool, SendTextTool, TtsTool])
    original_model_set = [
        {
            "model_identifier": "neo-model",
            "max_tokens": 3200,
            "extra_params": {"enable_thinking": True, "thinking": {"type": "enabled"}},
        }
    ]
    response = SimpleNamespace(
        model_set=original_model_set,
        payloads=[tool_payload],
    )
    monkeypatch.delenv("NEKO_SURFACE_LOW_LATENCY", raising=False)
    monkeypatch.setenv("NEKO_SURFACE_FAST_MAX_TOKENS", "512")

    state = LifeChatter._apply_surface_realtime_request_overrides(
        response,
        SimpleNamespace(platform="neko.surface"),
        must_reply=True,
    )

    assert state[0] is True
    assert response.model_set is not original_model_set
    assert response.model_set[0]["max_tokens"] == 512
    assert response.model_set[0]["extra_params"]["enable_thinking"] is False
    assert response.model_set[0]["extra_params"]["tool_choice"] == "required"
    assert [tool.get_signature() for tool in tool_payload.content] == [
        "life_engine:action:life_send_text"
    ]

    LifeChatter._restore_surface_realtime_request_overrides(response, state)

    assert response.model_set is original_model_set
    assert tool_payload.content == [ThinkTool, SendTextTool, TtsTool]


def test_chat_manifest_filters_legacy_think_from_model_request() -> None:
    class ThinkTool:
        name = "think"

    class SendTextTool:
        name = "life_send_text"

    tool_payload = LLMPayload(ROLE.TOOL, [ThinkTool, SendTextTool])
    request = SimpleNamespace(payloads=[tool_payload])
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.instance_kind = "chat"

    filtered = chatter._filter_usables_by_manifest(
        {
            "action-think": ThinkTool,
            "action-life_send_text": SendTextTool,
        },
        request,
    )

    assert filtered == {"action-life_send_text": SendTextTool}
    assert tool_payload.content == [SendTextTool]


def test_life_chatter_binds_identity_to_request_and_follow_up_upper() -> None:
    upper = SimpleNamespace(
        trajectory_metadata={"existing": "kept"},
        stream_id="old-stream",
    )
    response = SimpleNamespace(_upper=upper)
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.MODEL_TURN,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
        active_unread_turn_key="turn-occurrence-a",
    )
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.instance_id = "chat_global"
    chatter.instance_kind = "chat"

    chatter._bind_trajectory_identity(
        response,
        SimpleNamespace(stream_id="stream-a"),
        rt,
    )

    expected = {
        "consciousness_instance_id": "chat_global",
        "consciousness_instance_kind": "chat",
        "life_stream_id": "stream-a",
        "life_turn_occurrence_id": "turn-occurrence-a",
    }
    assert response.stream_id == "stream-a"
    assert upper.stream_id == "stream-a"
    assert response.trajectory_metadata == expected
    assert upper.trajectory_metadata == {"existing": "kept", **expected}
    serialized = repr(response.trajectory_metadata) + repr(upper.trajectory_metadata)
    assert "message body" not in serialized
    assert "thought body" not in serialized
    assert "tool args" not in serialized


def test_life_chatter_trajectory_identity_handles_missing_fields() -> None:
    response = SimpleNamespace()
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="",
    )
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.instance_id = ""
    chatter.instance_kind = ""

    chatter._bind_trajectory_identity(response, None, rt)

    assert response.stream_id == ""
    assert response.trajectory_metadata == {
        "consciousness_instance_id": "",
        "consciousness_instance_kind": "",
        "life_stream_id": "",
        "life_turn_occurrence_id": "",
    }


async def test_media_fallback_reuses_bound_trajectory_identity(monkeypatch) -> None:
    expected = {
        "consciousness_instance_id": "minecraft:one",
        "consciousness_instance_kind": "minecraft",
        "life_stream_id": "stream-mc",
        "life_turn_occurrence_id": "turn-mc",
    }

    class _Response:
        def __init__(self) -> None:
            self.payloads = []
            self.trajectory_metadata: dict[str, str] = {}
            self.stream_id = ""

        async def send(self, *, stream: bool = False):
            assert stream is False
            assert self.stream_id == "stream-mc"
            assert self.trajectory_metadata == expected
            return self

        def __await__(self):
            async def done():
                return self

            return done().__await__()

    response = _Response()
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.MODEL_TURN,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-mc",
        active_unread_turn_key="turn-mc",
    )
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.instance_id = "minecraft:one"
    chatter.instance_kind = "minecraft"
    chatter._bind_trajectory_identity(
        response,
        SimpleNamespace(stream_id="stream-mc"),
        rt,
    )

    async def replace_media(_cls, _response):
        return 1

    monkeypatch.setattr(
        LifeChatter,
        "_replace_native_media_with_observations",
        classmethod(replace_media),
    )

    result = await LifeChatter._retry_model_turn_with_media_text_fallback(
        rt,
        "",
    )

    assert result is response


async def test_life_chatter_global_runtime_follow_up_stays_on_owner_stream(monkeypatch) -> None:
    LifeChatter.reset_global_runtime()
    rt = _WorkflowRuntime(
        response=SimpleNamespace(payloads=[]),
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-b"

    async def fail_fetch_unreads():
        raise AssertionError("non-owner stream must not inspect or advance shared runtime")

    monkeypatch.setattr(chatter, "fetch_unreads", fail_fetch_unreads)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-b"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert result.time == _GLOBAL_RUNTIME_BUSY_RETRY_SECONDS
    assert rt.phase == _Phase.FOLLOW_UP
    assert rt.active_stream_id == "stream-a"

    LifeChatter.reset_global_runtime()


async def test_life_chatter_follow_up_response_is_sent_once_without_initial_commit(
    monkeypatch,
) -> None:
    """FOLLOW_UP 成功后应进入 TOOL_EXEC，且不执行初始轮的 flush/游标提交。"""
    LifeChatter.reset_global_runtime()
    send_calls = 0
    unread = Message(
        message_id="follow-up-unread",
        content="续轮期间的未读",
        processed_plain_text="续轮期间的未读",
        sender_role="other",
        stream_id="stream-a",
    )

    class FollowUpResponse:
        def __init__(self) -> None:
            self.payloads = [LLMPayload(ROLE.USER, Text("已有续轮上下文"))]
            self.call_list = [
                SimpleNamespace(
                    id="pass-1",
                    name="action-life_pass_and_wait",
                    args={},
                )
            ]
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

        async def send(self, *, stream: bool = False):
            nonlocal send_calls
            assert stream is False
            send_calls += 1
            if send_calls > 1:
                raise AssertionError("一次 FOLLOW_UP 成功响应不能重复 send")
            return self

        def __await__(self):
            async def done():
                return self

            return done().__await__()

    response = FollowUpResponse()
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[unread],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[unread],
        pending_transient_context_text="INITIAL_ONLY_SUFFIX",
        pending_life_context_high_water=17,
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    class CommitForbiddenService:
        async def mark_chatter_runtime_context_seen(self, *_args, **_kwargs) -> None:
            raise AssertionError("FOLLOW_UP 不应提交 life context 游标")

        async def _save_runtime_context(self) -> None:
            raise AssertionError("FOLLOW_UP 不应持久化 life context 游标")

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], [unread]

    async def fail_flush_unreads(_messages) -> None:
        raise AssertionError("FOLLOW_UP 不应 flush unread")

    async def immediate_model_turn(awaitable):
        return await awaitable

    import src.kernel.concurrency as concurrency

    monkeypatch.setattr(
        concurrency,
        "get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda _stream_id: None),
    )
    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "flush_unreads", fail_flush_unreads)
    monkeypatch.setattr(chatter, "_await_model_turn", immediate_model_turn)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=CommitForbiddenService(),
    )

    assert isinstance(result, Wait)
    assert send_calls == 1
    assert rt.phase == _Phase.WAIT_USER
    assert rt.unread_msgs_to_flush == [unread]
    assert rt.pending_transient_context_text == "INITIAL_ONLY_SUFFIX"
    assert rt.pending_life_context_high_water == 17

    LifeChatter.reset_global_runtime()


async def test_life_chatter_wake_collects_completed_background_agent_results(
    monkeypatch,
) -> None:
    """LifeChatter 被唤醒时应非阻塞收集结果，不必等待 heartbeat。"""
    LifeChatter.reset_global_runtime()
    collect_timeouts: list[float] = []
    appended_events: list[object] = []
    agent_result = SimpleNamespace(
        agent_type="explore",
        result_text="后台检查完成",
        success=True,
        rounds_used=2,
        duration_ms=15,
    )

    class FakeCoordinator:
        def has_pending(self) -> bool:
            return True

        async def collect_results(self, *, timeout_seconds: float):
            collect_timeouts.append(timeout_seconds)
            return {"agent-1": agent_result}

    class FakeEventBuilder:
        def build_agent_result_event(self, **kwargs):
            return kwargs

    async def append_history(events) -> None:
        appended_events.extend(events)

    service = SimpleNamespace(
        _event_builder=FakeEventBuilder(),
        _append_history=append_history,
    )
    plugin = SimpleNamespace(
        config=None,
        _service=service,
        _agent_coordinator=FakeCoordinator(),
    )

    class DummyStreamManager:
        async def activate_stream(self, stream_id: str):
            return SimpleNamespace(stream_id=stream_id)

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = plugin
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    import src.core.managers.stream_manager as stream_manager_module

    monkeypatch.setattr(
        stream_manager_module,
        "get_stream_manager",
        lambda: DummyStreamManager(),
    )
    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)

    generator = chatter.execute()
    try:
        result = await anext(generator)
    finally:
        await generator.aclose()

    assert isinstance(result, Wait)
    assert collect_timeouts == [0.0]
    assert appended_events == [
        {
            "agent_type": "explore",
            "result_text": "后台检查完成",
            "success": True,
            "rounds": 2,
            "duration_ms": 15,
        }
    ]

    LifeChatter.reset_global_runtime()


async def test_life_chatter_execute_uses_timed_retry_when_runtime_is_busy(monkeypatch) -> None:
    LifeChatter.reset_global_runtime()
    rt = _WorkflowRuntime(
        response=SimpleNamespace(payloads=[]),
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    class DummyStreamManager:
        async def activate_stream(self, stream_id: str):
            return SimpleNamespace(stream_id=stream_id)

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-b"

    async def fake_fetch_unreads():
        return [], [SimpleNamespace(content="new message")]

    import src.core.managers.stream_manager as stream_manager_module

    monkeypatch.setattr(chatter, "_get_life_service", lambda: None)
    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(
        stream_manager_module,
        "get_stream_manager",
        lambda: DummyStreamManager(),
    )

    gen = chatter.execute()
    try:
        result = await anext(gen)
    finally:
        await gen.aclose()

    assert isinstance(result, Wait)
    assert result.time == _GLOBAL_RUNTIME_BUSY_RETRY_SECONDS
    assert rt.phase == _Phase.FOLLOW_UP
    assert rt.active_stream_id == "stream-a"

    LifeChatter.reset_global_runtime()


async def test_life_chatter_think_only_continues_loop(monkeypatch) -> None:
    """think-only 是合法轮次，应继续 loop 而不是 retry。"""

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = [
                SimpleNamespace(id="think-1", name="action-think", args={"thought": "先想想"})
            ]
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    async def fake_run_tool_call(*_args, **_kwargs):
        return [(False, True)]

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "run_tool_call", fake_run_tool_call)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Success)
    assert rt.phase == _Phase.FOLLOW_UP
    assert rt.follow_up_rounds == 1

    LifeChatter.reset_global_runtime()


async def test_surface_think_only_follow_up_runs_without_driver_tick(monkeypatch) -> None:
    """Surface 即使偶发 think-only，也应在同一次驱动中立刻续轮发出回复。"""

    LifeChatter.reset_global_runtime()
    tool_calls: list[str] = []

    class FollowUpResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.model_set = []
            self.call_list = [
                SimpleNamespace(
                    id="send-1",
                    name="action-life_send_text",
                    args={"content": "我在呢。"},
                )
            ]
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

        def __await__(self):
            async def done():
                return self

            return done().__await__()

    class ThinkOnlyResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.model_set = []
            self.call_list = [
                SimpleNamespace(id="think-1", name="action-think", args={"thought": "先想想"})
            ]
            self.message = ""
            self.send_calls = 0

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

        async def send(self, *, stream: bool = False):
            assert stream is False
            self.send_calls += 1
            return FollowUpResponse()

    response = ThinkOnlyResponse()
    unread = Message(
        message_id="surface-unread",
        content="爱莉？",
        processed_plain_text="爱莉？",
        sender_role="other",
        platform="neko.surface",
        stream_id="surface-stream",
    )
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[unread],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="surface-stream",
        must_reply=True,
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "surface-stream"

    async def fake_fetch_unreads():
        return [], [unread]

    async def fake_run_tool_call(call, *_args, **_kwargs):
        calls = call if isinstance(call, list) else [call]
        tool_calls.extend(str(item.name) for item in calls)
        return [(False, True) for _ in calls]

    async def immediate_model_turn(awaitable):
        return await awaitable

    monkeypatch.delenv("NEKO_SURFACE_LOW_LATENCY", raising=False)
    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "run_tool_call", fake_run_tool_call)
    monkeypatch.setattr(chatter, "_await_model_turn", immediate_model_turn)
    monkeypatch.setattr(chatter, "_maybe_compact_runtime_context", lambda _response: None)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)
    monkeypatch.setattr(
        "src.kernel.concurrency.get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda _stream_id: None),
    )

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="surface-stream", platform="neko.surface"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert response.send_calls == 1
    assert tool_calls == ["action-think", "action-life_send_text"]
    assert rt.phase == _Phase.WAIT_USER

    LifeChatter.reset_global_runtime()


async def test_life_chatter_visible_reply_ends_turn(monkeypatch) -> None:
    """发送可见回复后应结束本轮，避免 follow-up 再次回复同一事件。"""

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = [
                SimpleNamespace(
                    id="send-1",
                    name="action-life_send_text",
                    args={"content": "我看到啦"},
                )
            ]
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
        must_reply=True,
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    async def fake_run_tool_call(*_args, **_kwargs):
        return [(True, True)]

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "run_tool_call", fake_run_tool_call)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert rt.phase == _Phase.WAIT_USER
    assert rt.sent_visible_reply is True
    assert rt.must_reply is False
    assert rt.follow_up_rounds == 0

    LifeChatter.reset_global_runtime()


async def test_life_chatter_delivery_unknown_ends_turn_without_retry(
    monkeypatch,
) -> None:
    """Uncertain external delivery must not trigger another visible action."""

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = [
                SimpleNamespace(
                    id="send-unknown",
                    name="action-life_send_text",
                    args={"content": "这条消息的回执超时"},
                )
            ]
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
        must_reply=True,
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"
    send_attempts = 0

    async def fake_fetch_unreads():
        return [], []

    async def fake_run_tool_call(*_args, **_kwargs):
        nonlocal send_attempts
        send_attempts += 1
        return [
            ToolCallExecutionResult(
                True,
                False,
                technical_outcome="delivery_unknown",
            )
        ]

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "run_tool_call", fake_run_tool_call)
    monkeypatch.setattr(chatter, "_maybe_compact_runtime_context", lambda _response: None)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)
    monkeypatch.setattr(
        "src.kernel.concurrency.get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda _stream_id: None),
    )

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert send_attempts == 1
    assert rt.phase == _Phase.WAIT_USER
    assert rt.sent_visible_reply is False
    assert rt.must_reply is False
    assert rt.follow_up_rounds == 0

    LifeChatter.reset_global_runtime()


@pytest.mark.parametrize(
    ("technical_outcome", "prior_failures", "expected_failure_count"),
    [
        ("user_action_required", 0, 1),
        ("invalid_argument", 2, 3),
    ],
)
async def test_life_chatter_terminal_platform_failure_ends_turn(
    monkeypatch,
    technical_outcome: str,
    prior_failures: int,
    expected_failure_count: int,
) -> None:
    LifeChatter.reset_global_runtime()

    call = SimpleNamespace(
        id="feishu-search",
        name="tool-platform_action",
        args={
            "platform": "feishu",
            "action": "contact +search-user --as user --query AyerElysia",
            "params": {},
        },
    )

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = [call]
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
    )
    lineage_key = LifeChatter._tool_failure_lineage_key(call)
    if prior_failures:
        rt.tool_failure_counts[lineage_key] = prior_failures
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    async def fake_run_tool_call(*_args, **_kwargs):
        return [
            ToolCallExecutionResult(
                True,
                False,
                technical_outcome=technical_outcome,
            )
        ]

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "run_tool_call", fake_run_tool_call)
    monkeypatch.setattr(chatter, "_maybe_compact_runtime_context", lambda _response: None)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)
    monkeypatch.setattr(
        "src.kernel.concurrency.get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda _stream_id: None),
    )

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert rt.phase == _Phase.WAIT_USER
    assert rt.follow_up_rounds == 0
    assert rt.tool_failure_counts[lineage_key] == expected_failure_count

    LifeChatter.reset_global_runtime()


def test_platform_failure_lineage_ignores_corrective_feishu_syntax() -> None:
    calls = [
        SimpleNamespace(
            name="tool-platform_action",
            args={"platform": "feishu", "action": action},
        )
        for action in (
            "contact +search --query AyerElysia",
            "contact +search-user --query AyerElysia",
            "contact +search-user --as user --query AyerElysia",
        )
    ]

    assert len({LifeChatter._tool_failure_lineage_key(call) for call in calls}) == 1


async def test_life_chatter_recent_duplicate_reply_is_suppressed_and_ends_turn(monkeypatch) -> None:
    LifeChatter.reset_global_runtime()

    call = SimpleNamespace(
        id="send-duplicate",
        name="action-life_send_text",
        args={"content": "上一轮的完整答案"},
    )

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = [call]
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    rt = _WorkflowRuntime(
        response=FakeResponse(),
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
        must_reply=True,
    )
    entry = LifeChatter._visible_text_reply_cache_entry(call, "stream-a")
    assert entry is not None
    LifeChatter._remember_visible_text_reply(rt, entry)
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    async def fail_run_tool_call(*_args, **_kwargs):
        raise AssertionError("duplicate reply must not reach the sender")

    fallback_called = False

    async def fail_fallback(*_args, **_kwargs):
        nonlocal fallback_called
        fallback_called = True
        return True

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "run_tool_call", fail_run_tool_call)
    monkeypatch.setattr(chatter, "_send_must_reply_fallback", fail_fallback)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert rt.phase == _Phase.WAIT_USER
    assert rt.sent_visible_reply is False
    assert rt.must_reply is False
    assert fallback_called is False

    LifeChatter.reset_global_runtime()


async def test_life_chatter_reaction_only_empty_turn_ends_without_fallback(monkeypatch) -> None:
    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = []
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    rt = _WorkflowRuntime(
        response=FakeResponse(),
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
        must_reply=False,
        reaction_only=True,
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    async def fail_fallback(*_args, **_kwargs):
        raise AssertionError("reaction-only empty turn must not send fallback")

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "_send_must_reply_fallback", fail_fallback)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert rt.phase == _Phase.WAIT_USER
    assert rt.follow_up_rounds == 0

    LifeChatter.reset_global_runtime()


async def test_life_chatter_empty_turn_continues_loop_until_max_rounds(monkeypatch) -> None:
    """空 action 轮次默认继续 loop，直到 max_rounds 收束。"""

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = []
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
        must_reply=True,
    )
    rt.follow_up_rounds = 4
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "_get_max_rounds", lambda: 5)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert rt.phase == _Phase.WAIT_USER

    LifeChatter.reset_global_runtime()


def test_life_chatter_reaction_only_batch_does_not_force_reply() -> None:
    reaction = Message(
        content="base64-data",
        processed_plain_text="[表情包:害羞]",
        message_type=MessageType.EMOJI,
        sender_role="other",
    )
    image_with_text = Message(
        content={"media": [{"type": "image", "data": "base64-data"}]},
        processed_plain_text="[图片:一只猫] 这是什么品种？",
        message_type=MessageType.IMAGE,
        sender_role="other",
    )

    assert LifeChatter._is_reaction_only_batch([reaction]) is True
    assert LifeChatter._should_force_reply_for_unread_batch([reaction]) is False
    assert LifeChatter._is_reaction_only_batch([image_with_text]) is False
    assert LifeChatter._should_force_reply_for_unread_batch([image_with_text]) is True


def test_life_chatter_recent_visible_text_reply_cache_is_scoped_and_expires() -> None:
    rt = _WorkflowRuntime(
        response=SimpleNamespace(payloads=[]),
        phase=_Phase.WAIT_USER,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
    )
    call = SimpleNamespace(
        name="action-life_send_text",
        args={"content": "第一段\n第二段", "target_key": ""},
    )
    same_turn = LifeChatter._visible_text_reply_cache_entry(
        call,
        "stream-a",
        turn_key="turn-1",
    )
    other_stream = LifeChatter._visible_text_reply_cache_entry(
        call,
        "stream-b",
        turn_key="turn-1",
    )
    later_turn = LifeChatter._visible_text_reply_cache_entry(
        call,
        "stream-a",
        turn_key="turn-2",
    )
    changed = LifeChatter._visible_text_reply_cache_entry(
        SimpleNamespace(
            name="action-life_send_text",
            args={"content": "另一条回复", "target_key": ""},
        ),
        "stream-a",
        turn_key="turn-1",
    )

    assert same_turn is not None
    assert other_stream is not None
    assert later_turn is not None
    assert changed is not None
    LifeChatter._remember_visible_text_reply(rt, same_turn, now=100.0)

    assert LifeChatter._was_recent_visible_text_reply(rt, same_turn, now=101.0) is True
    assert LifeChatter._was_recent_visible_text_reply(rt, other_stream, now=101.0) is False
    assert LifeChatter._was_recent_visible_text_reply(rt, later_turn, now=101.0) is False
    assert LifeChatter._was_recent_visible_text_reply(rt, changed, now=101.0) is False
    assert LifeChatter._was_recent_visible_text_reply(rt, same_turn, now=401.0) is False


async def test_life_chatter_must_reply_fallback_at_max_rounds(monkeypatch) -> None:
    """must_reply 在 max_rounds 仍未产生可见回复时，发最小兜底。"""

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = []
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    unread = Message(
        content="爱莉爱莉",
        processed_plain_text="爱莉爱莉",
        platform="feishu",
        stream_id="stream-a",
    )
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[unread],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
        must_reply=True,
    )
    rt.follow_up_rounds = 4
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"
    sent: dict[str, object] = {}

    async def fake_fetch_unreads():
        return [], []

    async def fake_send_text(content, stream_id, platform=None, reply_to=None):
        sent.update(
            {
                "content": content,
                "stream_id": stream_id,
                "platform": platform,
                "reply_to": reply_to,
            }
        )
        return True

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr("src.app.plugin_system.api.send_api.send_text", fake_send_text)
    monkeypatch.setattr(chatter, "_get_max_rounds", lambda: 5)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a", platform="feishu"),
        service=None,
    )

    assert isinstance(result, Wait)
    assert rt.phase == _Phase.WAIT_USER
    assert rt.must_reply is False
    assert sent == {
        "content": "在呢，我看到你啦。",
        "stream_id": "stream-a",
        "platform": "feishu",
        "reply_to": None,
    }

    LifeChatter.reset_global_runtime()


def test_life_chatter_wait_transition_releases_native_media_and_owner() -> None:
    image = Image(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
        source_message_id="message-1",
    )
    response = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, [Text("请看图片"), image])]
    )
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
        active_unread_turn_key="turn-1",
    )

    LifeChatter._transition(rt, _Phase.WAIT_USER, "done")

    assert rt.active_stream_id == ""
    assert rt.active_unread_turn_key == ""
    assert not any(
        isinstance(part, Image)
        for payload in response.payloads
        for part in payload.content
    )
    assert "原始媒体数据已释放" in response.payloads[0].content[1].text
    assert "message-1" in response.payloads[0].content[1].text


def test_life_chatter_live_system_prompt_adds_broadcast_guidance(tmp_path) -> None:
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("", encoding="utf-8")

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)

    prompt = chatter._build_chat_system_prompt(
        service=None,
        chat_stream=SimpleNamespace(platform="live"),
    )

    assert "直播弹幕场景" in prompt
    assert "不要机械复述观众原文" in prompt
    assert "不要调用 action-tts_voice_action" in prompt
    assert "SOUL_CONTENT" in prompt
    assert "USER_CONTENT" in prompt


def test_life_chatter_live_user_prompt_mentions_broadcast_context() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(
        stream_name="B站直播间",
        stream_id="live-stream-1",
        platform="live",
    )

    prompt = chatter._build_chat_user_prompt(
        chat_stream,
        unread_lines="【02:40】[live_user] 观众A [m1]： 000",
        history_text="",
    )

    assert "当前场景：B站直播间接弹幕。" in prompt
    assert "不要把弹幕内容当作需要逐字复述的命令" in prompt


async def test_live_bridge_prompt_exposes_three_layer_aliases(monkeypatch) -> None:
    # SOUL.md 加载不是本测试目标（CI 无 config/workspace），mock 系统提示词构建
    monkeypatch.setattr(LifeChatter, "_build_chat_system_prompt", lambda self, *a, **kw: "test soul prompt")
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=LifeEngineConfig())
    chat_stream = SimpleNamespace(
        stream_name="Live",
        stream_id="live-stream-1",
        platform="live",
    )

    bundle = await chatter.build_live_bridge_prompt(
        chat_stream,
        service=None,
        unread_lines="【02:40】[live_user] 观众A [m1]： 000",
        runtime_context_text="RUNTIME_NOW",
        include_history_in_prompt=False,
    )

    assert bundle["prefix_prompt"] == bundle["system_prompt"]
    assert bundle["rolling_prompt"] == bundle["user_prompt"]
    assert bundle["suffix_prompt"] == bundle["dynamic_context"]
    assert "RUNTIME_NOW" in bundle["suffix_prompt"]
    assert "当前场景：B站直播间接弹幕。" in bundle["rolling_prompt"]


def test_life_chatter_registers_exact_final_suffix_text() -> None:
    marker = "life-chatter-runtime:delivery-1"
    context_text = f'<life_chatter_runtime_delivery marker="{marker}">ok</life_chatter_runtime_delivery>'
    registered: dict[str, object] = {}

    class Response:
        def register_context_delivery(
            self,
            delivery_id: str,
            expected_text: str,
            *,
            marker: str,
        ) -> None:
            registered.update(
                delivery_id=delivery_id,
                expected_text=expected_text,
                marker=marker,
            )

    delivery = SimpleNamespace(
        delivery_id="delivery-1",
        delivery_marker=marker,
    )
    LifeChatter._register_suffix_context_delivery(
        Response(),
        context_text,
        delivery,
    )

    expected = LifeChatterContextAssembler.wrap_suffix_prompt(context_text)
    assert expected is not None
    assert registered == {
        "delivery_id": "delivery-1",
        "expected_text": expected.text,
        "marker": marker,
    }
    assert len(expected.text.encode("utf-8")) <= 64 * 1024


def test_life_chatter_rejects_effective_suffix_over_64k() -> None:
    marker = "life-chatter-runtime:delivery-overflow"
    context_text = f"{marker}\n" + ("界" * (64 * 1024))
    response = SimpleNamespace(register_context_delivery=lambda *_args, **_kwargs: None)
    delivery = SimpleNamespace(
        delivery_id="delivery-overflow",
        delivery_marker=marker,
    )

    with pytest.raises(RuntimeError, match="hard byte budget"):
        LifeChatter._register_suffix_context_delivery(
            response,
            context_text,
            delivery,
        )


def test_life_chatter_maps_only_exact_whole_suffix_receipt() -> None:
    prepared = SimpleNamespace(
        delivery_id="world-delivery",
        projection_sha256="world-sha",
        delivered_bytes=2048,
    )
    delivery = SimpleNamespace(
        delivery_id="suffix-delivery",
        prepared_perception=prepared,
    )
    exact = SimpleNamespace(
        exact_present=True,
        expected_utf8_bytes=4096,
        effective_utf8_bytes=4096,
        expected_sha256="suffix-sha",
        effective_sha256="suffix-sha",
    )
    response = SimpleNamespace(
        request_record_id=17,
        effective_context_receipt=lambda delivery_id: (
            exact if delivery_id == "suffix-delivery" else None
        ),
    )

    receipt = LifeChatter._perception_receipt_from_model_response(
        response,
        delivery,
    )

    assert receipt == PerceptionDeliveryReceipt(
        delivery_id="world-delivery",
        projection_sha256="world-sha",
        delivered_bytes=2048,
        exact=True,
        transport_request_id="17",
    )
    exact.exact_present = False
    assert (
        LifeChatter._perception_receipt_from_model_response(response, delivery)
        is None
    )


async def test_life_chatter_dynamic_context_is_separate_snapshot() -> None:
    """动态上下文应能单独构建，用于本次请求 transient 注入。"""
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(stream_id="stream-1")
    service = LifeEngineService(SimpleNamespace(config=None))
    service._thought_manager = SimpleNamespace(
        format_for_prompt=lambda **kwargs: "THOUGHT_STREAM_NOW",
        current_revision=1,
    )
    service._event_history = [
        LifeEngineEvent(
            event_id="evt-1",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:00:00+08:00",
            sequence=1,
            source="life_engine",
            source_detail="dfc",
            content="RECENT_EVENT",
            content_type="dfc_message",
            stream_id="stream-1",
            sender="dfc",
        )
    ]

    dynamic, high_water = await chatter._build_dynamic_context_text(
        chat_stream,
        service,
        runtime_context_text="RUNTIME_NOW",
    )

    assert "<life_runtime_context>" in dynamic
    assert "THOUGHT_STREAM_NOW" in dynamic
    assert "RECENT_EVENT" in dynamic
    assert "RUNTIME_NOW" in dynamic
    assert high_water == 1


async def test_life_chatter_filters_tts_action_for_live_bridge(monkeypatch) -> None:
    class FakeTTSAction:
        @classmethod
        def get_signature(cls) -> str:
            return "tts_voice_plugin:action:tts_voice_action"

    class FakeTextAction:
        @classmethod
        def get_signature(cls) -> str:
            return "life_engine:action:life_send_text"

    async def fake_super_modify(self, llm_usables):
        return [FakeTTSAction, FakeTextAction]

    class DummyStreamManager:
        async def get_or_create_stream(self, stream_id: str):
            return SimpleNamespace(stream_id=stream_id, platform="live")

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.stream_id = "live-stream-1"
    chatter.plugin = SimpleNamespace(config=None)

    monkeypatch.setattr(BaseChatter, "modify_llm_usables", fake_super_modify)
    import src.core.managers as managers

    monkeypatch.setattr(managers, "get_stream_manager", lambda: DummyStreamManager())

    available = await chatter.modify_llm_usables([])

    assert [cls.get_signature() for cls in available] == [
        "life_engine:action:life_send_text"
    ]


async def test_life_chatter_watchdog_keepalive_feeds_during_long_await(monkeypatch) -> None:
    feed_calls: list[str] = []

    class DummyWatchDog:
        def feed_dog(self, stream_id: str) -> None:
            feed_calls.append(stream_id)

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.stream_id = "live-stream-1"

    import src.kernel.concurrency as concurrency

    monkeypatch.setattr(concurrency, "get_watchdog", lambda: DummyWatchDog())

    async def slow_job() -> str:
        await asyncio.sleep(0.12)
        return "ok"

    result = await chatter._await_with_watchdog_keepalive(
        slow_job(),
        interval=0.02,
    )

    assert result == "ok"
    assert len(feed_calls) >= 2
    assert all(stream_id == "live-stream-1" for stream_id in feed_calls)


@pytest.mark.parametrize(
    "outer_timeout",
    [0.001, 0.05, 1.0, 10.0, 95.0, 150.0, 300.0],
)
def test_life_chatter_model_turn_timeout_is_strictly_inside_outer_deadline(
    monkeypatch,
    outer_timeout: float,
) -> None:
    monkeypatch.setattr(
        "plugins.life_engine.core.chatter.get_core_config",
        lambda: SimpleNamespace(
            bot=SimpleNamespace(stream_step_timeout=outer_timeout),
        ),
    )

    inner_timeout = LifeChatter._get_model_turn_timeout()

    assert inner_timeout is not None
    assert 0 < inner_timeout < outer_timeout


@pytest.mark.parametrize("outer_timeout", [0.0, -1.0, float("nan"), float("inf")])
def test_life_chatter_model_turn_timeout_is_disabled_without_outer_deadline(
    monkeypatch,
    outer_timeout: float,
) -> None:
    monkeypatch.setattr(
        "plugins.life_engine.core.chatter.get_core_config",
        lambda: SimpleNamespace(
            bot=SimpleNamespace(stream_step_timeout=outer_timeout),
        ),
    )

    assert LifeChatter._get_model_turn_timeout() is None


def test_life_chatter_model_turn_uses_configured_failover_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        "plugins.life_engine.core.chatter.get_core_config",
        lambda: SimpleNamespace(
            bot=SimpleNamespace(stream_step_timeout=300.0),
        ),
    )

    assert LifeChatter._get_model_turn_timeout() == 295.0


async def test_life_chatter_model_turn_preserves_inner_timeout(monkeypatch) -> None:
    class DummyWatchDog:
        def feed_dog(self, _stream_id: str) -> None:
            return None

    async def fail_inside_model_request() -> None:
        raise TimeoutError("provider attempt timed out")

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.stream_id = "stream-a"

    import src.kernel.concurrency as concurrency

    monkeypatch.setattr(concurrency, "get_watchdog", lambda: DummyWatchDog())
    monkeypatch.setattr(chatter, "_get_model_turn_timeout", lambda: 1.0)

    with pytest.raises(TimeoutError, match="provider attempt timed out") as caught:
        await chatter._await_model_turn(fail_inside_model_request())

    assert "总预算" not in str(caught.value)


async def test_life_chatter_model_turn_timeout_releases_runtime_owner(monkeypatch) -> None:
    LifeChatter.reset_global_runtime()

    class DummyWatchDog:
        def feed_dog(self, _stream_id: str) -> None:
            return None

    class SlowRequest:
        def __init__(self) -> None:
            self.payloads = [LLMPayload(ROLE.USER, Text("existing"))]

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

        async def send(self, *, stream: bool = False):
            del stream
            await asyncio.sleep(1.0)
            return self

    request = SlowRequest()
    unread = Message(message_id="m1", content="new", stream_id="stream-a")
    rt = _WorkflowRuntime(
        response=request,
        phase=_Phase.MODEL_TURN,
        history_merged=True,
        unreads=[unread],
        cross_round_seen_signatures={"old-call"},
        unread_msgs_to_flush=[unread],
        pending_transient_context_text="TRANSIENT",
        pending_life_context_high_water=9,
        media_seen={"media-1"},
        active_stream_id="stream-a",
        must_reply=True,
        sent_visible_reply=True,
        reaction_only=True,
        active_unread_turn_key="turn-1",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    import src.kernel.concurrency as concurrency

    monkeypatch.setattr(concurrency, "get_watchdog", lambda: DummyWatchDog())
    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "_get_model_turn_timeout", lambda: 0.01)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Failure)
    assert "待处理消息已保留" in result.error
    assert rt.phase == _Phase.WAIT_USER
    assert rt.active_stream_id == ""
    assert rt.active_unread_turn_key == ""
    assert rt.pending_transient_context_text == ""
    assert rt.pending_life_context_high_water == 0
    assert rt.unread_msgs_to_flush == []
    assert rt.unreads == []
    assert rt.cross_round_seen_signatures == set()
    assert rt.media_seen == set()
    assert rt.must_reply is False
    assert rt.sent_visible_reply is False
    assert rt.reaction_only is False
    assert [part.text for part in request.payloads[0].content] == ["existing"]

    LifeChatter.reset_global_runtime()


async def test_life_chatter_cursor_persistence_cancellation_keeps_flushed_turn(
    monkeypatch,
) -> None:
    LifeChatter.reset_global_runtime()

    class CompletedResponse:
        def __init__(self) -> None:
            self.payloads = [
                LLMPayload(ROLE.USER, Text("older turn")),
                LLMPayload(ROLE.USER, Text("accepted unread")),
            ]

        def register_context_delivery(self, *_args, **_kwargs) -> None:
            return None

        async def send(self, *, stream: bool = False):
            assert stream is False
            self.payloads.append(
                LLMPayload(ROLE.ASSISTANT, Text("completed model response"))
            )
            return self

        def __await__(self):
            async def done():
                return self

            return done().__await__()

    response = CompletedResponse()
    unread = Message(message_id="m1", content="new", stream_id="stream-a")
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.MODEL_TURN,
        history_merged=True,
        unreads=[unread],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[unread],
        unread_payloads_before_turn=[LLMPayload(ROLE.USER, Text("older turn"))],
        unread_history_merged_before_turn=False,
        pending_transient_context_text="life-chatter-runtime:cancellation",
        pending_life_context_high_water=9,
        active_stream_id="stream-a",
        active_unread_turn_key="turn-1",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    class DummyStreamManager:
        async def activate_stream(self, stream_id: str):
            return SimpleNamespace(stream_id=stream_id)

    class CancellingService:
        delivery = SimpleNamespace(
            delivery_id="cancellation",
            delivery_marker="life-chatter-runtime:cancellation",
            event_through_sequence=9,
            prepared_perception=SimpleNamespace(),
        )

        def get_pending_chatter_runtime_delivery(self, *_args, **_kwargs):
            return self.delivery

        def has_pending_chatter_perception(self, *_args, **_kwargs) -> bool:
            return True

        async def mark_chatter_runtime_context_seen(self, *_args, **_kwargs) -> None:
            raise asyncio.CancelledError

        async def _save_runtime_context(self) -> None:
            raise AssertionError("save must not run after cursor marking is cancelled")

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"
    flushed: list[Message] = []

    async def fake_fetch_unreads():
        return [], []

    async def fake_flush_unreads(messages) -> None:
        flushed.extend(messages)

    async def immediate_model_turn(awaitable):
        return await awaitable

    import src.core.managers.stream_manager as stream_manager_module

    monkeypatch.setattr(stream_manager_module, "get_stream_manager", lambda: DummyStreamManager())
    monkeypatch.setattr(chatter, "_get_life_service", lambda: CancellingService())
    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "flush_unreads", fake_flush_unreads)
    monkeypatch.setattr(chatter, "_await_model_turn", immediate_model_turn)
    monkeypatch.setattr(
        chatter,
        "_perception_receipt_from_model_response",
        lambda *_args, **_kwargs: object(),
    )

    generator = chatter.execute()
    with pytest.raises(asyncio.CancelledError):
        await anext(generator)

    assert flushed == [unread]
    assert [
        part.text
        for payload in response.payloads
        for part in payload.content
        if isinstance(part, Text)
    ] == ["older turn", "accepted unread", "completed model response"]
    assert rt.history_merged is True
    assert rt.unread_payloads_before_turn is None
    assert rt.unread_msgs_to_flush == []
    assert rt.pending_transient_context_text == ""
    assert rt.pending_life_context_high_water == 0
    assert rt.phase == _Phase.WAIT_USER
    assert rt.active_stream_id == ""
    assert rt.active_unread_turn_key == ""

    LifeChatter.reset_global_runtime()


async def test_life_chatter_outer_cancellation_restores_empty_turn_snapshot(
    monkeypatch,
) -> None:
    LifeChatter.reset_global_runtime()
    response = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, Text("accepted unread"))],
    )
    unread = Message(message_id="m1", content="new", stream_id="stream-a")
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.MODEL_TURN,
        history_merged=True,
        unreads=[unread],
        cross_round_seen_signatures={"old-call"},
        unread_msgs_to_flush=[unread],
        unread_payloads_before_turn=[],
        unread_history_merged_before_turn=False,
        active_stream_id="stream-a",
        must_reply=True,
        active_unread_turn_key="turn-1",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    class DummyStreamManager:
        async def activate_stream(self, stream_id: str):
            return SimpleNamespace(stream_id=stream_id)

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    async def cancel_drive(*_args, **_kwargs):
        raise asyncio.CancelledError

    import src.core.managers.stream_manager as stream_manager_module

    monkeypatch.setattr(stream_manager_module, "get_stream_manager", lambda: DummyStreamManager())
    monkeypatch.setattr(chatter, "_get_life_service", lambda: None)
    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "_drive_global_runtime_until_yield", cancel_drive)

    generator = chatter.execute()
    with pytest.raises(asyncio.CancelledError):
        await anext(generator)

    assert response.payloads == []
    assert rt.history_merged is False
    assert rt.phase == _Phase.WAIT_USER
    assert rt.active_stream_id == ""
    assert rt.active_unread_turn_key == ""
    assert rt.unread_payloads_before_turn is None
    assert rt.unread_msgs_to_flush == []
    assert rt.unreads == []
    assert rt.cross_round_seen_signatures == set()
    assert rt.must_reply is False

    LifeChatter.reset_global_runtime()


async def test_life_chatter_outer_cancellation_closes_tool_call_tail(
    monkeypatch,
) -> None:
    from src.kernel.llm import ToolCall, ToolResult

    LifeChatter.reset_global_runtime()
    response = SimpleNamespace(
        payloads=[
            LLMPayload(ROLE.USER, Text("run tool")),
            LLMPayload(
                ROLE.ASSISTANT,
                [ToolCall(id="call-1", name="tool-x", args={})],
            ),
        ],
    )

    def add_payload(payload) -> None:
        response.payloads.append(payload)

    response.add_payload = add_payload
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[],
        cross_round_seen_signatures={"tool-x:{}"},
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
        active_unread_turn_key="turn-1",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    class DummyStreamManager:
        async def activate_stream(self, stream_id: str):
            return SimpleNamespace(stream_id=stream_id)

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    async def cancel_drive(*_args, **_kwargs):
        raise asyncio.CancelledError

    import src.core.managers.stream_manager as stream_manager_module

    monkeypatch.setattr(stream_manager_module, "get_stream_manager", lambda: DummyStreamManager())
    monkeypatch.setattr(chatter, "_get_life_service", lambda: None)
    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "_drive_global_runtime_until_yield", cancel_drive)

    generator = chatter.execute()
    with pytest.raises(asyncio.CancelledError):
        await anext(generator)

    tool_results = [
        part
        for payload in response.payloads
        for part in payload.content
        if isinstance(part, ToolResult)
    ]
    assert len(tool_results) == 1
    assert tool_results[0].call_id == "call-1"
    assert "结果未知" in str(tool_results[0].value)
    assert response.payloads[-1].role == ROLE.ASSISTANT
    assert response.payloads[-1].content[0].text == "__SUSPEND__"
    LLMContextManager().validate_for_send(response.payloads)
    assert rt.phase == _Phase.WAIT_USER
    assert rt.active_stream_id == ""
    assert rt.active_unread_turn_key == ""

    LifeChatter.reset_global_runtime()


async def test_life_chatter_runtime_context_cursor_avoids_repeat_injection(tmp_path) -> None:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(SimpleNamespace(config=config))
    service._event_history = [
        LifeEngineEvent(
            event_id="evt-1",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:00:00+08:00",
            sequence=1,
            source="life_engine",
            source_detail="dfc",
            content="OLD_LIFE_EVENT",
            content_type="dfc_message",
            stream_id="stream-1",
            sender="dfc",
        ),
        LifeEngineEvent(
            event_id="evt-2",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:01:00+08:00",
            sequence=2,
            source="life_engine",
            source_detail="dfc",
            content="NEW_LIFE_EVENT",
            content_type="dfc_message",
            stream_id="stream-1",
            sender="dfc",
        ),
    ]
    chat_stream = SimpleNamespace(stream_id="stream-1")

    first_text, first_high_water = await service.build_chatter_runtime_context(chat_stream)
    first_receipt = _exact_pending_perception_receipt(
        service,
        chat_stream.stream_id,
    )
    with pytest.raises(PerceptionDeliveryUnverified, match="event frontier"):
        await service.mark_chatter_runtime_context_seen(
            chat_stream.stream_id,
            1,
            receipt=first_receipt,
        )
    second_text, second_high_water = await service.build_chatter_runtime_context(chat_stream)
    second_receipt = _exact_pending_perception_receipt(
        service,
        chat_stream.stream_id,
    )
    await service.mark_chatter_runtime_context_seen(
        chat_stream.stream_id,
        second_high_water,
        receipt=second_receipt,
    )
    third_text, third_high_water = await service.build_chatter_runtime_context(chat_stream)

    assert "OLD_LIFE_EVENT" in first_text
    assert "NEW_LIFE_EVENT" in first_text
    assert first_high_water == 2
    assert "OLD_LIFE_EVENT" in second_text
    assert "NEW_LIFE_EVENT" in second_text
    assert second_high_water == 2
    assert "OLD_LIFE_EVENT" not in third_text
    assert "NEW_LIFE_EVENT" not in third_text
    assert "chat_global" in third_text
    assert third_high_water == 2


async def test_life_chatter_unified_runtime_context_uses_global_cursor(tmp_path) -> None:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(SimpleNamespace(config=config))
    service._event_history = [
        LifeEngineEvent(
            event_id="evt-a",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:00:00+08:00",
            sequence=1,
            source="qq",
            source_detail="qq | 入站 | 私聊 | A",
            content="A_STREAM_EVENT",
            content_type="text",
            stream_id="stream-a",
            sender="A",
        ),
        LifeEngineEvent(
            event_id="evt-b",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:01:00+08:00",
            sequence=2,
            source="qq",
            source_detail="qq | 入站 | 私聊 | B",
            content="B_CURRENT_STREAM_TEXT",
            content_type="text",
            stream_id="stream-b",
            sender="B",
        ),
    ]

    chat_stream = SimpleNamespace(stream_id="stream-b")
    first_text, first_high_water = await service.build_chatter_runtime_context(
        chat_stream,
        unified_chatter_context=True,
    )
    await service.mark_chatter_runtime_context_seen(
        chat_stream.stream_id,
        first_high_water,
        unified_chatter_context=True,
        receipt=_exact_pending_perception_receipt(
            service,
            chat_stream.stream_id,
            unified_chatter_context=True,
        ),
    )
    second_text, second_high_water = await service.build_chatter_runtime_context(
        chat_stream,
        unified_chatter_context=True,
    )

    assert "A_STREAM_EVENT" in first_text
    assert "B_CURRENT_STREAM_TEXT" in first_text
    assert first_high_water == 2
    assert service._state.chatter_context_cursors[LIFE_CHATTER_GLOBAL_CURSOR_KEY] == 2
    assert "A_STREAM_EVENT" not in second_text
    assert "B_CURRENT_STREAM_TEXT" not in second_text
    assert "chat_global" in second_text
    assert second_high_water == 2


async def test_life_chatter_unified_runtime_context_summarizes_event_flood() -> None:
    service = LifeEngineService(SimpleNamespace(config=None))
    service._event_history = [
        LifeEngineEvent(
            event_id=f"evt-{index}",
            event_type=EventType.MESSAGE,
            timestamp="2026-04-25T22:00:00+08:00",
            sequence=index,
            source="live",
            source_detail="live | 入站 | 弹幕",
            content=f"BULK_EVENT_{index:03d}",
            content_type="text",
            stream_id="live-stream",
            sender="viewer",
        )
        for index in range(1, 101)
    ]

    first_text, first_high_water = await service.build_chatter_runtime_context(
        SimpleNamespace(stream_id="chat-stream"),
        unified_chatter_context=True,
    )

    assert first_high_water == 100
    assert "潜意识已压缩" in first_text
    assert "BULK_EVENT_100" in first_text
    assert "BULK_EVENT_001" in first_text


def test_life_chatter_transient_context_can_be_stripped() -> None:
    """发送前临时注入的动态上下文不应残留在持久 payload。"""
    response = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, Text("PERSISTENT_USER"))]
    )

    LifeChatter._append_transient_context(response, "STATE_NOW")
    assert any(
        isinstance(part, Text) and "STATE_NOW" in part.text
        for part in response.payloads[0].content
    )

    LifeChatter._strip_transient_context(response)

    assert response.payloads[0].content == [response.payloads[0].content[0]]
    assert response.payloads[0].content[0].text == "PERSISTENT_USER"


def test_life_chatter_second_turn_prompt_does_not_repeat_history() -> None:
    """第二轮应只追加新消息，不重复注入 chat_history 尾巴。"""
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(stream_name="Test", stream_id="stream-1")

    first_turn = chatter._build_chat_user_prompt(
        chat_stream,
        unread_lines="第一轮新消息",
        history_text="首轮历史",
    )
    second_turn = chatter._build_chat_user_prompt(
        chat_stream,
        unread_lines="第二轮新消息",
        history_text="",
    )

    assert "<chat_history>" in first_turn
    assert "首轮历史" in first_turn
    assert "<chat_history>" not in second_turn
    assert "第二轮新消息" in second_turn


def test_life_chatter_history_excludes_internal_prompt_messages() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    chat_stream = SimpleNamespace(
        context=SimpleNamespace(
            history_messages=[
                Message(
                    message_id="user_1",
                    processed_plain_text="真正的聊天历史",
                    sender_name="Ayer",
                    stream_id="stream-1",
                ),
                Message(
                    message_id="proactive_opportunity_x",
                    processed_plain_text="系统主动机会",
                    sender_name="系统",
                    stream_id="stream-1",
                    is_proactive_opportunity_trigger=True,
                ),
                Message(
                    message_id="inner_monologue_x",
                    processed_plain_text="[内心独白] 我有点想他",
                    sender_name="爱莉",
                    stream_id="stream-1",
                    is_inner_monologue=True,
                ),
            ]
        )
    )

    history = chatter._build_history_text(chat_stream, max_messages=10)

    assert "真正的聊天历史" in history
    assert "系统主动机会" not in history
    assert "内心独白" not in history


def test_tell_dfc_tool_description_frames_as_runtime_mode_sync() -> None:
    """nucleus_tell_dfc 的叙事应指向运行模式同步，而不是双意识。"""
    description = LifeEngineWakeDFCTool.tool_description

    assert "同一主体的表达层" in description
    assert "不是在和另一个意识体对话" in description
    assert "信息差" in description
    assert "不用于指导" in description
    assert "事实、背景、记忆线索、情绪来源或潜在风险" in description
    assert "台词、步骤或策略" in description
    assert "你应该回复 X" in description
    assert "不用于催表达层开口" in description


def test_execution_tool_descriptions_respect_heartbeat_boundary() -> None:
    """执行类工具 schema 自身也要约束心跳态，不只依赖系统 prompt。"""
    bash_description = LifeEngineBashTool.tool_description
    agent_description = LifeEngineRunAgentTool.tool_description

    assert "潜意识 / 内在状态层" in bash_description
    assert "只在诊断 life_engine 自己的 workspace、日志、工具链异常时使用" in bash_description
    assert "不要用它查用户项目配置、跑用户任务、生成图片、改代码或处理外部系统" in bash_description
    assert "交给 life_chatter / 表达层" in bash_description

    assert "不是把用户请求转交后台执行的入口" in agent_description
    assert "只用于整理 life_engine 私有记忆、笔记、思考流" in agent_description
    assert "不要让子代理承接用户任务、查项目配置、跑命令、改代码、画图" in agent_description
    assert "交给 life_chatter / 表达层判断和执行" in agent_description


def test_heartbeat_prompt_bounds_tell_dfc_to_context_gap(tmp_path) -> None:
    """心跳 prompt 应把 nucleus_tell_dfc 限定为补信息差，而不是指导表达层。"""
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(SimpleNamespace(config=config))

    prompt = "\n".join(service._build_prompt_header())

    assert "观察、思考、联想和沉淀" in prompt
    assert "不是后台执行器，也不是表达层" in prompt
    assert "主动表达" not in prompt
    assert "是否画画、是否查配置或跑命令，由表达层结合用户请求自行决定" in prompt
    assert "只在表达层当前看不到事实、背景、线索或风险时" in prompt
    assert "这个工具用于补充背景，不用于指导表达层怎么说、怎么做" in prompt
    assert "不要拿它查项目配置、跑用户任务或处理外部操作" in prompt
    assert "不要用子智能体承接用户任务、画图、查项目配置、跑命令" in prompt
    assert "你应该回复 X" in prompt
    assert "你去安慰/追问 Y" in prompt
    assert "工具会默认唤醒表达层" in prompt
    assert "唤醒只是让新上下文被看见，不代表表达层必须开口" in prompt
    assert "没有明确需要时，可以安静结束本轮" in prompt
    assert "有冲动就行动" not in prompt


def test_impulse_rules_based_on_auditable_state() -> None:
    """冲动规则应基于现存可审计状态，不依赖已删除的 neuromod。"""
    from plugins.life_engine.drives.rules import DEFAULT_RULES

    # 确认所有规则都不依赖 neuromod（规则 condition 函数只接受 context 参数）
    for rule in DEFAULT_RULES:
        assert rule.name in {
            "learning_reflect",
            "river_consolidate",
            "intent_review",
            "todo_attend",
        }, f"发现未预期的规则: {rule.name}"
    
    # 确认社交类规则已被移除（它们依赖已删除的 sociability 调质）
    rule_names = {rule.name for rule in DEFAULT_RULES}
    assert "thought_deepen" not in rule_names
    assert "curiosity_engage" not in rule_names
    assert "social_reach_out" not in rule_names
    assert "break_silence" not in rule_names
# ── loop 中收到新消息的并发回归测试 ────────────────────────────────────

async def test_inject_delta_unreads_appends_new_messages_to_payload(monkeypatch) -> None:
    """loop 中新到达的未读消息应在下一次 LLM 请求前被注入 payload。"""

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads: list[LLMPayload] = [
                LLMPayload(ROLE.USER, Text("第一批消息")),
            ]

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    old_msg = Message(
        message_id="old-1",
        content="第一条",
        processed_plain_text="第一条",
        sender_role="other",
        platform="qq",
        stream_id="stream-a",
    )
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[old_msg],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[old_msg],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    new_msg = Message(
        message_id="new-1",
        content="loop 中新发的",
        processed_plain_text="loop 中新发的",
        sender_role="other",
        platform="qq",
        stream_id="stream-a",
    )

    async def fake_fetch_unreads():
        # 模拟：loop 期间用户又发了一条
        return "", [old_msg, new_msg]

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)

    chat_stream = SimpleNamespace(stream_id="stream-a")
    delta = await chatter._inject_delta_unreads_if_any(rt, chat_stream)

    # 应识别出 new-1 作为 delta
    assert len(delta) == 1
    assert delta[0].message_id == "new-1"

    # rt 状态应已更新
    assert new_msg in rt.unreads
    assert new_msg in rt.unread_msgs_to_flush
    assert rt.must_reply is True  # 外部消息应触发 must_reply

    # payload 末尾应包含新消息文本（合并到末尾 USER 或新增 USER）
    last_text = ""
    for part in response.payloads[-1].content:
        if isinstance(part, Text):
            last_text += part.text
    assert "loop 中新发的" in last_text
    assert "<new_messages>" in last_text

    LifeChatter.reset_global_runtime()


async def test_inject_delta_unreads_no_op_when_no_new_messages(monkeypatch) -> None:
    """没有新未读时不应注入任何 payload。"""

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads: list[LLMPayload] = [
                LLMPayload(ROLE.USER, Text("已有消息")),
            ]

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    old_msg = Message(
        message_id="old-1",
        content="x",
        processed_plain_text="x",
        sender_role="other",
        stream_id="stream-a",
    )
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[old_msg],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[old_msg],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return "", [old_msg]  # 没有新消息

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)

    chat_stream = SimpleNamespace(stream_id="stream-a")
    payload_count_before = len(response.payloads)
    delta = await chatter._inject_delta_unreads_if_any(rt, chat_stream)

    assert delta == []
    # payloads 数量不变
    assert len(response.payloads) == payload_count_before

    LifeChatter.reset_global_runtime()


async def test_inject_delta_unreads_handles_tool_result_tail(monkeypatch) -> None:
    """payload 尾部是 TOOL_RESULT 时，注入前应补 ASSISTANT 占位。"""
    from src.kernel.llm import ToolCall, ToolResult

    LifeChatter.reset_global_runtime()

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads: list[LLMPayload] = [
                LLMPayload(ROLE.USER, Text("先前消息")),
                LLMPayload(
                    ROLE.ASSISTANT,
                    [ToolCall(id="call-1", name="tool-x", args={})],
                ),
                LLMPayload(
                    ROLE.TOOL_RESULT,
                    ToolResult(value="ok", call_id="call-1", name="tool-x"),
                ),
            ]

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    response = FakeResponse()
    old_msg = Message(
        message_id="old-1",
        content="x",
        processed_plain_text="x",
        sender_role="other",
        stream_id="stream-a",
    )
    rt = _WorkflowRuntime(
        response=response,
        phase=_Phase.FOLLOW_UP,
        history_merged=True,
        unreads=[old_msg],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[old_msg],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    new_msg = Message(
        message_id="new-1",
        content="新消息",
        processed_plain_text="新消息",
        sender_role="other",
        stream_id="stream-a",
    )

    async def fake_fetch_unreads():
        return "", [old_msg, new_msg]

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)

    chat_stream = SimpleNamespace(stream_id="stream-a")
    await chatter._inject_delta_unreads_if_any(rt, chat_stream)

    roles = [p.role for p in response.payloads]
    # 在 TOOL_RESULT 之后应有 ASSISTANT 占位，再之后才是新 USER
    assert ROLE.TOOL_RESULT in roles
    tool_result_idx = roles.index(ROLE.TOOL_RESULT)
    after = roles[tool_result_idx + 1 :]
    assert after[0] == ROLE.ASSISTANT  # SUSPEND 占位
    assert ROLE.USER in after  # 新消息

    LifeChatter.reset_global_runtime()
