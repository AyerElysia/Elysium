"""life_engine 对话提示词与叙事测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import plugins.life_engine.core.chatter as chatter_module
from plugins.life_engine.constants import LIFE_CHATTER_GLOBAL_CURSOR_KEY
from plugins.life_engine.core.chatter import (
    _GLOBAL_RUNTIME_BUSY_RETRY_SECONDS,
    LifeChatter,
    _Phase,
    _WorkflowRuntime,
)
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.context_assembly import LifeChatterContextAssembler
from plugins.life_engine.core.context_compaction import (
    SUMMARY_CLOSE,
    SUMMARY_INTRO,
    SUMMARY_OPEN,
)
from plugins.life_engine.service.core import LifeEngineService
from plugins.life_engine.service.event_builder import EventType, LifeEngineEvent
from plugins.life_engine.service.perception_gateway import (
    PerceptionDeliveryReceipt,
    PerceptionDeliveryUnverified,
)
from plugins.life_engine.tools.exec_tools import LifeEngineBashTool
from plugins.life_engine.tools.file_tools import LifeEngineRunAgentTool
from src.core.components.base.chatter import BaseChatter, Failure, Success, Wait
from src.core.config.core_config import CoreConfig
from src.core.models.media import MediaAttachment
from src.core.models.message import Message, MessageType
from src.core.utils.llm_tool_call import ToolCallExecutionResult
from src.kernel.llm import (
    ROLE,
    Image,
    LLMContextManager,
    LLMPayload,
    ReasoningText,
    Text,
    ToolCall,
    ToolRegistry,
    ToolResult,
)
from src.kernel.storage import canonical_json_sha256

_TEST_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


def _service_plugin(config: LifeEngineConfig | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        config=config,
        global_storage_config=CoreConfig(
            storage=CoreConfig.StorageSection(backend="local")
        ),
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


@pytest.mark.asyncio
async def test_life_chatter_system_prompt_includes_memory_and_chatter_tools_not_heartbeat_tool(
    tmp_path,
) -> None:
    """聊天态应共享 SOUL/USER/MEMORY/EXISTENCE/TOOLS，并保留核心工具说明。"""
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
    (tmp_path / "EXISTENCE.md").write_text("EXISTENCE_CONTENT", encoding="utf-8")

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)
    prompt = await chatter._build_chat_system_prompt(service=None)

    assert "SOUL_CONTENT" in prompt
    assert "USER_CONTENT" in prompt
    assert "MEMORY_DURABLE" in prompt
    assert "MEMORY_ACTIVE" in prompt
    assert "MEMORY_FADING" not in prompt
    assert "给编辑者看的说明" not in prompt
    assert "TOOL_CONTENT" not in prompt
    assert "EXISTENCE_CONTENT" in prompt
    assert "CHATTER_TOOLS_CONTENT" in prompt
    assert "assistant 纯文本 **不会被发送给用户**" in prompt
    assert "自己的内心独白" in prompt
    assert "action-life_pass_and_wait" in prompt
    assert "life_send_text" in prompt
    assert "reason" in prompt
    assert "action-think" not in LifeChatter._build_primary_tool_guide()
    assert "nucleus_browser_fetch" in LifeChatter._build_primary_tool_guide()
    assert "nucleus_web_search" in LifeChatter._build_primary_tool_guide()
    assert "nucleus_apply_patch" in LifeChatter._build_primary_tool_guide()
    assert "nucleus_glob_file" in LifeChatter._build_primary_tool_guide()
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


def test_life_chatter_context_hook_projects_only_content_neutral_refs() -> None:
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
    assert "<mechanical_context_omission>" in compressed
    assert "ctxg_" in compressed
    assert "旧用户消息" not in compressed
    assert "旧回复" not in compressed
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
    assert '"unlisted_earlier_group_count":1' in compressed
    assert '"listed_group_count":1' in compressed
    assert "第一组用户消息" not in compressed
    assert "第二组用户消息" not in compressed
    assert "ctxg_" in compressed


def test_life_chatter_rolling_context_snapshot_uses_mechanical_refs() -> None:
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
    assert "<mechanical_context_omission>" in compacted[0].content[0].text
    assert "ctxg_" in compacted[0].content[0].text
    assert "旧用户消息" not in compacted[0].content[0].text
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
    assert "<mechanical_context_omission>" in compacted[0].content[0].text
    assert "超大消息" not in compacted[0].content[0].text


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
    assert "[图片]" not in serialized
    assert "[工具结果]" not in serialized
    assert "<mechanical_context_omission>" in serialized
    assert "最新问题" in serialized


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


def test_life_chatter_snapshot_digest_survives_canonical_storage_round_trip() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    snapshot = LifeChatter._snapshot_data_for_payloads(
        [LLMPayload(ROLE.USER, Text("保持内容不变"))]
    )
    canonical_round_trip = json.loads(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )

    restored = chatter._deserialize_rolling_context_snapshot(canonical_round_trip)

    assert len(restored) == 1
    assert restored[0].content[0].text == "保持内容不变"


def test_life_chatter_v2_digest_requires_verified_outer_integrity_after_canonicalization() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    snapshot = LifeChatter._snapshot_data_for_payloads(
        [LLMPayload(ROLE.USER, Text("旧版快照"))]
    )
    snapshot["version"] = 2
    payload_json = json.dumps(
        snapshot["payloads"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    snapshot["payload_digest"] = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    canonical_round_trip = json.loads(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )

    with pytest.raises(RuntimeError, match="RollingContextPayloadDigestMismatch"):
        chatter._deserialize_rolling_context_snapshot(canonical_round_trip)

    restored = chatter._deserialize_rolling_context_snapshot(
        canonical_round_trip,
        outer_integrity_verified=True,
    )
    assert restored[0].content[0].text == "旧版快照"


def test_life_chatter_snapshot_digest_rejects_changed_content() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    snapshot = LifeChatter._snapshot_data_for_payloads(
        [LLMPayload(ROLE.USER, Text("原始内容"))]
    )
    snapshot["payloads"][0]["content"][0]["text"] = "被篡改的内容"

    with pytest.raises(RuntimeError, match="RollingContextPayloadDigestMismatch"):
        chatter._deserialize_rolling_context_snapshot(snapshot)


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
    config.chatter.rolling_context_snapshot_char_budget = 1_000
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
    assert result.triggered is False
    assert compactable_response.payloads is not payloads
    assert compactable_response.payloads == payloads

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
    async def fake_system_prompt(self, *args, **kwargs):
        del self, args, kwargs
        return "test soul"

    monkeypatch.setattr(LifeChatter, "_build_chat_system_prompt", fake_system_prompt)

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


def _life_chatter_group_mention_message() -> Message:
    return Message(
        message_id="group-mention-1",
        content="@<爱莉希雅:3427056465> 晚安啦～♪好爱莉",
        processed_plain_text="@<爱莉希雅:3427056465> 晚安啦～♪好爱莉",
        message_type=MessageType.TEXT,
        sender_role="other",
        stream_id="group-stream",
        extra={"at_users": [{"user_id": "3427056465"}]},
        raw_data={"self_id": "3427056465"},
    )


async def test_life_chatter_group_mention_bypasses_router_llm(monkeypatch) -> None:
    chatter = _life_chatter_for_config(LifeEngineConfig())
    unread = _life_chatter_group_mention_message()
    chat_stream = SimpleNamespace(
        stream_id="group-stream",
        platform="qq",
        chat_type="group",
    )

    async def must_not_read_history(*_args, **_kwargs):
        raise AssertionError("被 @ 的消息不应先构建路由历史")

    monkeypatch.setattr(chatter, "_build_history_text_async", must_not_read_history)

    decision = await chatter._should_respond(
        "T9033: @<爱莉希雅:3427056465> 晚安啦～♪好爱莉",
        [unread],
        chat_stream,
    )

    assert decision["should_respond"] is True
    assert "交给表达层判断如何回应" in decision["reason"]
    assert decision["force_reply"] is True


async def test_life_chatter_execute_backstop_routes_mid_turn_arrivals(
    monkeypatch,
) -> None:
    """回合进行期间到达的消息必须在挂起前被路由，而不是无限期滞留。

    复现 2026-08-29 事故：群消息“晚安各位”触发路由并沉默时，被 @ 的
    “晚安啦～♪好爱莉”尚未进入未读队列（落库晚于读取）。旧逻辑直接
    Wait(None) 挂起，其唤醒事件已被本回合消费，消息整夜无人处理。
    """

    LifeChatter.reset_global_runtime()
    LifeChatter._GLOBAL_USABLE_MAP = {}
    message_a = Message(
        message_id="group-goodnight",
        content="晚安各位",
        processed_plain_text="晚安各位",
        message_type=MessageType.TEXT,
        sender_role="other",
        stream_id="stream-a",
    )
    message_b = _life_chatter_group_mention_message()
    unread_pool: list[Message] = [message_a]
    flushed: list[Message] = []
    router_calls: list[list[str]] = []

    async def fetch_unreads(*_args, **_kwargs):
        return "text", list(unread_pool)

    async def flush_unreads(messages):
        flushed.extend(messages)
        removed = {msg.message_id for msg in messages}
        unread_pool[:] = [
            msg for msg in unread_pool if msg.message_id not in removed
        ]

    async def router_false(unread_lines, unread_msgs, _chat_stream):
        router_calls.append([msg.message_id for msg in unread_msgs])
        if all(msg.message_id != "group-mention-1" for msg in unread_msgs):
            unread_pool.append(message_b)
        return {"reason": "普通群晚安", "should_respond": False}

    chat_stream = SimpleNamespace(
        stream_id="stream-a",
        stream_name="Test",
        platform="test",
        chat_type="group",
        bot_id="3427056465",
        context=SimpleNamespace(history_messages=[]),
    )

    class _FakeStreamManager:
        async def activate_stream(self, _stream_id):
            return chat_stream

    async def no_history(*_args, **_kwargs):
        return ""

    async def no_dynamic_context(*_args, **_kwargs):
        return "", 0

    async def noop_async(*_args, **_kwargs):
        return None

    def noop(*_args, **_kwargs):
        return None

    chatter = _life_chatter_for_config(LifeEngineConfig())
    rt = _WorkflowRuntime(
        response=_RouterBranchRequest(flushed, must_not_send=True),
        phase=_Phase.WAIT_USER,
        history_merged=False,
        unreads=[],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
    )
    LifeChatter._GLOBAL_RUNTIME = rt

    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: _FakeStreamManager(),
    )
    monkeypatch.setattr(chatter, "_get_life_service", lambda: None)
    monkeypatch.setattr(chatter, "fetch_unreads", fetch_unreads)
    monkeypatch.setattr(chatter, "flush_unreads", flush_unreads)
    monkeypatch.setattr(chatter, "_should_respond", router_false)
    monkeypatch.setattr(chatter, "_build_history_text_async", no_history)
    monkeypatch.setattr(chatter, "_build_dynamic_context_text", no_dynamic_context)
    monkeypatch.setattr(
        chatter, "_collect_completed_background_agent_results", noop_async
    )
    monkeypatch.setattr(chatter, "_collect_pending_stream_turns", noop)
    monkeypatch.setattr(chatter, "_commit_consumed_stream_turns", noop_async)
    monkeypatch.setattr(
        "src.kernel.concurrency.get_watchdog",
        lambda: SimpleNamespace(feed_dog=lambda _stream_id: None),
    )

    try:
        generator = chatter.execute()
        result = await generator.__anext__()
    finally:
        LifeChatter.reset_global_runtime()

    assert isinstance(result, Wait)
    assert router_calls == [
        ["group-goodnight"],
        ["group-mention-1"],
    ]
    assert [msg.message_id for msg in flushed] == [
        "group-goodnight",
        "group-mention-1",
    ]
    assert unread_pool == []


def test_router_fallback_prompt_hands_direct_mentions_to_expression() -> None:
    from plugins.life_engine.core.router import _fallback_prompt

    prompt = _fallback_prompt("爱莉希雅", "3427056465")

    assert "直接 @ 她的账号或点名她" in prompt
    assert "必须交给表达层" in prompt
    assert "不要写具体回复" in prompt


def test_chat_manifest_filters_legacy_think_from_model_request() -> None:
    class ThinkTool:
        name = "action-think"

    class SendTextTool:
        name = "action-life_send_text"

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


def test_chat_manifest_filters_real_tool_registry() -> None:
    class ThinkTool:
        name = "action-think"

    class SendTextTool:
        name = "action-life_send_text"

    registry = ToolRegistry()
    registry.register(ThinkTool, name="action-think")
    registry.register(SendTextTool, name="action-life_send_text")
    tool_payload = LLMPayload(ROLE.TOOL, [ThinkTool, SendTextTool])
    request = SimpleNamespace(payloads=[tool_payload])
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.instance_kind = "chat"

    filtered = chatter._filter_usables_by_manifest(registry, request)

    assert isinstance(filtered, ToolRegistry)
    assert filtered.get_all_names() == ["action-life_send_text"]
    assert tool_payload.content == [SendTextTool]


def test_rolling_context_projection_drops_retired_think_pair() -> None:
    payloads = [
        LLMPayload(
            ROLE.ASSISTANT,
            [
                Text("keep this live tool context"),
                ReasoningText("keep this live tool reasoning"),
                ToolCall(id="think-1", name="action-think", args={"thought": "legacy"}),
                ToolCall(
                    id="send-1",
                    name="action-life_send_text",
                    args={"thought": "current", "content": "hello"},
                ),
            ],
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            [
                ToolResult(value="legacy ok", call_id="think-1", name="action-think"),
                ToolResult(value="sent", call_id="send-1", name="action-life_send_text"),
            ],
        ),
    ]

    cleaned = LifeChatter._without_retired_think_history(payloads)

    assert cleaned[0].content[:2] == [
        Text("keep this live tool context"),
        ReasoningText("keep this live tool reasoning"),
    ]
    assert [
        part.name for part in cleaned[0].content if isinstance(part, ToolCall)
    ] == ["action-life_send_text"]
    assert [part.name for part in cleaned[1].content] == ["action-life_send_text"]


def test_rolling_context_projection_drops_whole_retired_think_assistant() -> None:
    payloads = [
        LLMPayload(ROLE.USER, Text("new message")),
        LLMPayload(
            ROLE.ASSISTANT,
            [
                Text("__SUSPEND__"),
                ReasoningText("legacy provider reasoning"),
                ToolCall(
                    id="think-only",
                    name="action-think",
                    args={"thought": "legacy"},
                ),
            ],
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(
                value="legacy ok",
                call_id="think-only",
                name="action-think",
            ),
        ),
        LLMPayload(
            ROLE.ASSISTANT,
            ToolCall(
                id="send-1",
                name="action-life_send_text",
                args={"thought": "current", "content": "hello"},
            ),
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(
                value="sent",
                call_id="send-1",
                name="action-life_send_text",
            ),
        ),
        LLMPayload(ROLE.ASSISTANT, Text("__SUSPEND__")),
    ]

    cleaned = LifeChatter._without_retired_think_history(payloads)

    assert [payload.role for payload in cleaned] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.ASSISTANT,
    ]
    assert LifeChatter._without_retired_think_history(cleaned) == cleaned
    assert all(
        getattr(part, "name", None) != "action-think"
        for payload in cleaned
        for part in payload.content
    )
    result_ids = {
        part.call_id
        for payload in cleaned
        for part in payload.content
        if isinstance(part, ToolResult)
    }
    call_ids = {
        part.id
        for payload in cleaned
        for part in payload.content
        if isinstance(part, ToolCall)
    }
    assert result_ids <= call_ids
    LLMContextManager()._validate_payloads(
        cleaned,
        allow_incomplete_tail=False,
    )


def test_rolling_context_projection_drops_retired_proactive_tool_chain() -> None:
    payloads = [
        LLMPayload(ROLE.USER, Text("请继续")),
        LLMPayload(
            ROLE.ASSISTANT,
            [
                Text("__SUSPEND__"),
                ToolCall(
                    id="legacy-proactive",
                    name="nucleus_manage_thought_stream",
                    args={"action": "advance"},
                ),
            ],
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(
                value="retired",
                call_id="legacy-proactive",
                name="nucleus_manage_thought_stream",
            ),
        ),
        LLMPayload(
            ROLE.ASSISTANT,
            ToolCall(
                id="canonical-proactive",
                name="nucleus_proactive_query",
                args={"resource": "attention"},
            ),
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(
                value="bounded projection",
                call_id="canonical-proactive",
                name="nucleus_proactive_query",
            ),
        ),
        LLMPayload(ROLE.ASSISTANT, Text("__SUSPEND__")),
    ]

    cleaned = LifeChatter._without_retired_proactive_history(payloads)

    assert [payload.role for payload in cleaned] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.ASSISTANT,
    ]
    assert LifeChatter._without_retired_proactive_history(cleaned) == cleaned
    names = [
        getattr(part, "name", "")
        for payload in cleaned
        for part in payload.content
        if isinstance(part, (ToolCall, ToolResult))
    ]
    assert names == ["nucleus_proactive_query", "nucleus_proactive_query"]
    LLMContextManager()._validate_payloads(
        cleaned,
        allow_incomplete_tail=False,
    )


def _payloads_text(payloads: list[LLMPayload]) -> str:
    chunks: list[str] = []
    for payload in payloads:
        for part in payload.content:
            for attr in ("text", "name", "value"):
                value = getattr(part, attr, None)
                if value:
                    chunks.append(str(value))
            args = getattr(part, "args", None)
            if args:
                chunks.append(str(args))
    return "\n".join(chunks)


def test_rolling_context_projection_drops_prefixed_retired_proactive_tool_chain() -> None:
    payloads = [
        LLMPayload(ROLE.USER, Text("请继续")),
        LLMPayload(
            ROLE.ASSISTANT,
            [
                Text("__SUSPEND__"),
                ToolCall(
                    id="legacy-proactive",
                    name="tool-nucleus_manage_thought_stream",
                    args={"action": "advance"},
                ),
            ],
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(
                value="retired",
                call_id="legacy-proactive",
                name="tool-nucleus_manage_thought_stream",
            ),
        ),
        LLMPayload(
            ROLE.ASSISTANT,
            ToolCall(
                id="canonical-proactive",
                name="tool-nucleus_proactive_query",
                args={"resource": "attention"},
            ),
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(
                value="bounded projection",
                call_id="canonical-proactive",
                name="tool-nucleus_proactive_query",
            ),
        ),
        LLMPayload(ROLE.ASSISTANT, Text("__SUSPEND__")),
    ]

    cleaned = LifeChatter._without_retired_proactive_history(payloads)

    assert [payload.role for payload in cleaned] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.ASSISTANT,
    ]
    names = [
        getattr(part, "name", "")
        for payload in cleaned
        for part in payload.content
        if isinstance(part, (ToolCall, ToolResult))
    ]
    assert names == ["tool-nucleus_proactive_query", "tool-nucleus_proactive_query"]
    LLMContextManager()._validate_payloads(
        cleaned,
        allow_incomplete_tail=False,
    )


def _inner_return_rolling_prompt(*, extra_unread: str = "") -> str:
    unread = (
        "【12:00】<other> [life_engine_inner_return] 系统（潜意识回声）： "
        "这是你自己沉下去的内心对话回声，不是用户，也不是外联。\n"
        "潜意识回声：秘密念头"
    )
    if extra_unread:
        unread = extra_unread.rstrip() + "\n" + unread
    return LifeChatterContextAssembler.build_rolling_prompt(
        stream_name="私聊",
        stream_id="stream-a",
        unread_lines=unread,
    )


def test_derived_rolling_keeps_wake_envelope_until_explicit_strip() -> None:
    prompt = _inner_return_rolling_prompt()
    payloads = [
        LLMPayload(ROLE.USER, Text(prompt)),
        LLMPayload(
            ROLE.ASSISTANT,
            ToolCall(
                id="send-1",
                name="action-life_send_text",
                args={"thought": "看见回声", "content": "看见了"},
            ),
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value="sent", call_id="send-1", name="action-life_send_text"),
        ),
        LLMPayload(ROLE.ASSISTANT, Text("__SUSPEND__")),
    ]

    live = LifeChatter._derived_rolling_payloads(
        payloads,
        strip_wake_envelopes=False,
    )
    live_text = _payloads_text(live)
    assert "life_engine_inner_return" in live_text
    assert "秘密念头" in live_text

    closed = LifeChatter._derived_rolling_payloads(
        payloads,
        strip_wake_envelopes=True,
    )
    closed_text = _payloads_text(closed)
    assert "life_engine_inner_return" not in closed_text
    assert "秘密念头" not in closed_text
    names = [
        getattr(part, "name", "")
        for payload in closed
        for part in payload.content
        if isinstance(part, (ToolCall, ToolResult))
    ]
    assert names == ["action-life_send_text", "action-life_send_text"]
    LLMContextManager()._validate_payloads(
        closed,
        allow_incomplete_tail=False,
    )

    snapshot = json.dumps(
        LifeChatter._snapshot_data_for_payloads(payloads),
        ensure_ascii=False,
    )
    assert "life_engine_inner_return" not in snapshot
    assert "秘密念头" not in snapshot
    assert "action-life_send_text" in snapshot


def test_derived_rolling_keeps_real_user_line_when_stripping_wake_envelope() -> None:
    prompt = _inner_return_rolling_prompt(
        extra_unread="【12:00】<member> [u123] 小星星： 你好呀",
    )
    payloads = [
        LLMPayload(ROLE.USER, Text("更早的真实对话")),
        LLMPayload(ROLE.ASSISTANT, Text("上一句回应")),
        LLMPayload(ROLE.USER, Text(prompt)),
        LLMPayload(
            ROLE.ASSISTANT,
            ToolCall(
                id="send-1",
                name="action-life_send_text",
                args={"content": "在的"},
            ),
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value="sent", call_id="send-1", name="action-life_send_text"),
        ),
    ]

    closed = LifeChatter._derived_rolling_payloads(
        payloads,
        strip_wake_envelopes=True,
    )
    closed_text = _payloads_text(closed)
    assert "你好呀" in closed_text
    assert "更早的真实对话" in closed_text
    assert "life_engine_inner_return" not in closed_text
    assert "秘密念头" not in closed_text
    LLMContextManager()._validate_payloads(
        closed,
        allow_incomplete_tail=False,
    )


def test_derived_rolling_strips_initiative_wake_envelope() -> None:
    prompt = LifeChatterContextAssembler.build_rolling_prompt(
        stream_name="私聊",
        stream_id="stream-a",
        unread_lines=(
            "【12:00】<other> [life_engine_initiative] 系统（主体主动外联）： "
            "主体刚刚明确选择发起一次外联。\n"
            "主体公开意向：想她了"
        ),
    )
    payloads = [
        LLMPayload(ROLE.USER, Text(prompt)),
        LLMPayload(
            ROLE.ASSISTANT,
            ToolCall(
                id="pass-1",
                name="action-life_pass_and_wait",
                args={},
            ),
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value="wait", call_id="pass-1", name="action-life_pass_and_wait"),
        ),
        LLMPayload(ROLE.ASSISTANT, Text("__SUSPEND__")),
    ]

    closed = LifeChatter._derived_rolling_payloads(
        payloads,
        strip_wake_envelopes=True,
    )
    closed_text = _payloads_text(closed)
    assert "life_engine_initiative" not in closed_text
    assert "想她了" not in closed_text
    LLMContextManager()._validate_payloads(
        closed,
        allow_incomplete_tail=False,
    )


def test_wait_user_transition_strips_wake_envelope_from_live_payloads() -> None:
    prompt = _inner_return_rolling_prompt()
    response = SimpleNamespace(
        payloads=[
            LLMPayload(ROLE.USER, Text(prompt)),
            LLMPayload(
                ROLE.ASSISTANT,
                ToolCall(
                    id="send-1",
                    name="action-life_send_text",
                    args={"content": "看见了"},
                ),
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(
                    value="sent",
                    call_id="send-1",
                    name="action-life_send_text",
                ),
            ),
            LLMPayload(ROLE.ASSISTANT, Text("__SUSPEND__")),
        ]
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

    LifeChatter._transition(rt, _Phase.WAIT_USER, "pass")

    blob = _payloads_text(response.payloads)
    assert "life_engine_inner_return" not in blob
    assert "秘密念头" not in blob
    assert rt.phase == _Phase.WAIT_USER


def test_rolling_context_projection_drops_retired_reaction_guidance() -> None:
    legacy_hint = (
        "incoming emoji remains visible"
        + chatter_module._RETIRED_REACTION_HINT_SUFFIX
    )
    payloads = [
        LLMPayload(
            ROLE.USER,
            Text(
                "<reaction_only_hint>\n"
                "user-authored text must remain\n"
                "</reaction_only_hint>"
            ),
        ),
        LLMPayload(
            ROLE.ASSISTANT,
            Text("assistant context" + chatter_module._RETIRED_REACTION_HINT_SUFFIX),
        ),
        LLMPayload(ROLE.USER, Text(legacy_hint)),
        LLMPayload(ROLE.ASSISTANT, Text("keep assistant response")),
    ]

    cleaned = LifeChatter._without_retired_reaction_guidance(payloads)

    assert cleaned[0] is payloads[0]
    assert cleaned[1] is payloads[1]
    assert [part.text for part in cleaned[2].content] == [
        "incoming emoji remains visible"
    ]
    assert cleaned[3] is payloads[3]
    assert LifeChatter._without_retired_reaction_guidance(cleaned) == cleaned
    LLMContextManager()._validate_payloads(
        cleaned,
        allow_incomplete_tail=False,
    )


def test_rolling_context_projection_drops_only_strict_retired_summary() -> None:
    retired = LLMPayload(
        ROLE.USER,
        Text(f"{SUMMARY_INTRO}\n{SUMMARY_OPEN}\n旧复制正文\n{SUMMARY_CLOSE}"),
    )
    ordinary = LLMPayload(
        ROLE.USER,
        Text(f"我只是讨论 {SUMMARY_OPEN} 这个旧标签，不是系统摘要。"),
    )
    assistant = LLMPayload(ROLE.ASSISTANT, Text("保留这次真实回应"))

    cleaned = LifeChatter._without_retired_context_summaries(
        [retired, ordinary, assistant]
    )

    assert cleaned == [ordinary, assistant]
    assert LifeChatter._without_retired_context_summaries(cleaned) == cleaned
    assert "旧复制正文" not in str(cleaned)
    LLMContextManager()._validate_payloads(
        cleaned,
        allow_incomplete_tail=False,
    )


@pytest.mark.asyncio
async def test_rolling_context_legacy_think_load_is_read_only_then_save_normalizes(
    tmp_path,
) -> None:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)
    chatter.instance_id = "chat_global"

    legacy_payloads = [
        LLMPayload(
            ROLE.USER,
            Text(
                f"{SUMMARY_INTRO}\n{SUMMARY_OPEN}\n"
                "arbitrary copied tool result\n"
                f"{SUMMARY_CLOSE}"
            ),
        ),
        LLMPayload(
            ROLE.USER,
            Text(
                "new message"
                + chatter_module._RETIRED_REACTION_HINT_SUFFIX
            ),
        ),
        LLMPayload(
            ROLE.ASSISTANT,
            [
                Text("__SUSPEND__"),
                ToolCall(
                    id="think-only",
                    name="action-think",
                    args={"thought": "legacy"},
                ),
            ],
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(
                value="legacy ok",
                call_id="think-only",
                name="action-think",
            ),
        ),
        LLMPayload(
            ROLE.ASSISTANT,
            ToolCall(
                id="send-1",
                name="action-life_send_text",
                args={"thought": "current", "content": "hello"},
            ),
        ),
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(
                value="sent",
                call_id="send-1",
                name="action-life_send_text",
            ),
        ),
        LLMPayload(ROLE.ASSISTANT, Text("__SUSPEND__")),
    ]
    payload_items = [
        item
        for item in (
            LifeChatter._serialize_payload(payload)
            for payload in legacy_payloads
        )
        if item is not None
    ]
    payload_json = json.dumps(
        payload_items,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    snapshot = {
        "version": 2,
        "runtime_key": LIFE_CHATTER_GLOBAL_CURSOR_KEY,
        "payload_digest": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        "payloads": payload_items,
    }
    path = chatter._rolling_context_snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    original_bytes = path.read_bytes()

    loaded = await chatter._load_rolling_context_snapshot()

    assert path.read_bytes() == original_bytes
    assert [payload.role for payload in loaded] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.ASSISTANT,
    ]
    assert [part.text for part in loaded[0].content] == ["new message"]
    assert "arbitrary copied tool result" not in str(loaded)
    LLMContextManager()._validate_payloads(
        loaded,
        allow_incomplete_tail=False,
    )

    await chatter._save_rolling_context_snapshot(SimpleNamespace(payloads=loaded))

    normalized = json.loads(path.read_text(encoding="utf-8"))
    assert normalized["payload_digest"] == canonical_json_sha256(
        normalized["payloads"]
    )
    assert all(
        part.get("name") != "action-think"
        for payload in normalized["payloads"]
        for part in payload["content"]
    )
    assert "reaction_only_hint" not in json.dumps(
        normalized,
        ensure_ascii=False,
    )
    assert SUMMARY_OPEN not in json.dumps(normalized, ensure_ascii=False)


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


@pytest.mark.parametrize(
    ("call_name", "tool_success", "technical_outcome", "expected_outcome"),
    [
        ("action-life_send_text", True, "delivered", "spoke"),
        (
            "action-life_send_text",
            False,
            "delivery_unknown",
            "delivery_unknown",
        ),
        ("action-life_pass_and_wait", True, "", "passed"),
    ],
)
async def test_initiative_outreach_claims_before_send_and_commits_exact_terminal(
    monkeypatch,
    call_name: str,
    tool_success: bool,
    technical_outcome: str,
    expected_outcome: str,
) -> None:
    LifeChatter.reset_global_runtime()
    call = SimpleNamespace(
        id="outreach-action-1",
        name=call_name,
        args={"content": "我来找你啦"} if "send_text" in call_name else {},
    )

    class FakeResponse:
        def __init__(self) -> None:
            self.payloads = []
            self.call_list = [call]
            self.message = ""

        def add_payload(self, payload) -> None:
            self.payloads.append(payload)

    outreach = Message(
        message_id="initiative_outreach_stable",
        content="主体主动外联",
        processed_plain_text="主体主动外联",
        sender_role="other",
        platform="qq",
        stream_id="stream-a",
        is_initiative_outreach_trigger=True,
        initiative_outreach_occurrence_id="outreach:one",
    )
    rt = _WorkflowRuntime(
        response=FakeResponse(),
        phase=_Phase.TOOL_EXEC,
        history_merged=True,
        unreads=[outreach],
        cross_round_seen_signatures=set(),
        unread_msgs_to_flush=[],
        active_stream_id="stream-a",
    )
    LifeChatter._GLOBAL_RUNTIME = rt
    LifeChatter._GLOBAL_USABLE_MAP = {}
    order: list[str] = []

    class FakeService:
        async def claim_initiative_outreach_expressions(
            self,
            occurrences,
            *,
            action_id: str,
        ):
            order.append("claim")
            assert occurrences == ["outreach:one"]
            assert action_id == call.id
            return {"claimed": True, "execute_allowed": True}

        async def resolve_initiative_outreach_expressions(
            self,
            occurrences,
            *,
            outcome: str,
            action_id: str = "",
            delivery_receipt_sha256: str = "",
            delivery_message_id: str = "",
        ):
            order.append(f"resolve:{outcome}")
            assert occurrences == ["outreach:one"]
            if outcome != "passed":
                assert action_id == call.id
            if outcome == "spoke":
                assert delivery_receipt_sha256 == "a" * 64
                assert delivery_message_id == "platform-message:1"
            else:
                assert delivery_receipt_sha256 == ""
                assert delivery_message_id == ""
            return {"resolved_count": 1, "pending_count": 0}

    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=None)
    chatter.stream_id = "stream-a"

    async def fake_fetch_unreads():
        return [], []

    async def fake_run_tool_call(*_args, **_kwargs):
        order.append("platform")
        return [
            ToolCallExecutionResult(
                True,
                tool_success,
                technical_outcome=technical_outcome,
                delivery_receipt_sha256=(
                    "a" * 64 if technical_outcome == "delivered" else ""
                ),
                delivery_message_id=(
                    "platform-message:1"
                    if technical_outcome == "delivered"
                    else ""
                ),
                delivery_proof_status=(
                    "durable" if technical_outcome == "delivered" else ""
                ),
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
        service=FakeService(),  # type: ignore[arg-type]
    )

    assert isinstance(result, Wait)
    if call_name == "action-life_pass_and_wait":
        assert order == ["resolve:passed"]
    else:
        assert order[:2] == ["claim", "platform"]
        assert f"resolve:{expected_outcome}" in order
        assert outreach.extra["life_turn_scope"][
            "initiative_outreach_occurrences"
        ] == ["outreach:one"]
    assert rt.phase == _Phase.WAIT_USER
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


async def test_life_chatter_external_media_empty_turn_uses_standard_follow_up(monkeypatch) -> None:
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
        unreads=[
            Message(
                content="base64-data",
                processed_plain_text="[表情包:害羞]",
                message_type=MessageType.EMOJI,
                sender_role="other",
            )
        ],
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

    async def fail_fallback(*_args, **_kwargs):
        raise AssertionError("a first empty turn must schedule a normal follow-up")

    monkeypatch.setattr(chatter, "fetch_unreads", fake_fetch_unreads)
    monkeypatch.setattr(chatter, "_send_must_reply_fallback", fail_fallback)
    monkeypatch.setattr(chatter, "_save_rolling_context_snapshot", _skip_snapshot_save)

    result = await chatter._drive_global_runtime_until_yield(
        SimpleNamespace(stream_id="stream-a"),
        service=None,
    )

    assert isinstance(result, Success)
    assert rt.phase == _Phase.FOLLOW_UP
    assert rt.follow_up_rounds == 1
    assert rt.must_reply is True

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


def test_life_chatter_external_media_uses_same_reply_commitment_as_text() -> None:
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
    text = Message(
        content="hello",
        processed_plain_text="hello",
        message_type=MessageType.TEXT,
        sender_role="other",
    )
    voice = Message(
        content="voice-data",
        processed_plain_text="[voice transcript]",
        message_type=MessageType.VOICE,
        sender_role="other",
    )

    assert LifeChatter._should_force_reply_for_unread_batch([reaction]) is True
    assert LifeChatter._should_force_reply_for_unread_batch([image_with_text]) is True
    assert LifeChatter._should_force_reply_for_unread_batch([text]) is True
    assert LifeChatter._should_force_reply_for_unread_batch([voice]) is True
    assert (
        LifeChatter._should_force_reply_for_decision(
            {"should_respond": False},
            [reaction],
        )
        is False
    )
    assert (
        LifeChatter._should_force_reply_for_decision(
            {"should_respond": True},
            [reaction],
        )
        is True
    )


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


@pytest.mark.asyncio
async def test_life_chatter_selected_rolling_context_never_reads_or_writes_local(
    tmp_path: Path,
) -> None:
    class _RuntimeStore:
        def __init__(self) -> None:
            self.record = None
            self.writes: list[dict[str, object]] = []

        async def get_state(self, namespace: str, state_key: str):
            assert namespace == "life_chatter.rolling_context"
            assert state_key == "chat_global"
            return self.record

        async def put_state(self, **kwargs):
            self.writes.append(dict(kwargs))
            self.record = SimpleNamespace(
                revision=int(kwargs["expected_revision"]) + 1,
                payload=dict(kwargs["payload"]),
            )
            return self.record

    store = _RuntimeStore()
    service = SimpleNamespace(runtime_state_store=lambda: store)
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config, _service=service)
    chatter._rolling_context_state_revision = 0

    local_path = chatter._rolling_context_snapshot_path()
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text('{"version": 2, "payloads": "must-not-read"}', encoding="utf-8")

    assert await chatter._load_rolling_context_snapshot(service) == []
    response = SimpleNamespace(
        payloads=[LLMPayload(ROLE.USER, [Text("remote payload")])]
    )
    await chatter._save_rolling_context_snapshot(response)

    assert len(store.writes) == 1
    assert store.writes[0]["namespace"] == "life_chatter.rolling_context"
    assert store.writes[0]["state_key"] == "chat_global"
    assert chatter._rolling_context_state_revision == 1
    assert local_path.read_text(encoding="utf-8").endswith('"must-not-read"}')

    restored = await chatter._load_rolling_context_snapshot(service)
    assert len(restored) == 1
    assert restored[0].role == ROLE.USER
    assert isinstance(restored[0].content[0], Text)
    assert restored[0].content[0].text == "remote payload"


@pytest.mark.asyncio
async def test_life_chatter_selected_rolling_context_migrates_canonicalized_v2(
    tmp_path: Path,
) -> None:
    v3_snapshot = LifeChatter._snapshot_data_for_payloads(
        [LLMPayload(ROLE.USER, Text("selected v2 payload"))]
    )
    v3_snapshot["version"] = 2
    payload_json = json.dumps(
        v3_snapshot["payloads"],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    v3_snapshot["payload_digest"] = hashlib.sha256(
        payload_json.encode("utf-8")
    ).hexdigest()
    canonical_v2 = json.loads(
        json.dumps(v3_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    )

    class _RuntimeStore:
        def __init__(self) -> None:
            self.record = SimpleNamespace(revision=7, payload=canonical_v2)
            self.write = None

        async def get_state(self, namespace: str, state_key: str):
            assert (namespace, state_key) == (
                "life_chatter.rolling_context",
                "chat_global",
            )
            return self.record

        async def put_state(self, **kwargs):
            self.write = dict(kwargs)
            return SimpleNamespace(revision=8, payload=dict(kwargs["payload"]))

    store = _RuntimeStore()
    service = SimpleNamespace(runtime_state_store=lambda: store)
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config, _service=service)
    chatter._rolling_context_state_revision = 0

    restored = await chatter._load_rolling_context_snapshot(service)
    assert restored[0].content[0].text == "selected v2 payload"
    assert chatter._rolling_context_state_revision == 7

    await chatter._save_rolling_context_snapshot(SimpleNamespace(payloads=restored))
    assert store.write is not None
    assert store.write["expected_revision"] == 7
    assert store.write["schema_version"] == 3
    assert store.write["payload"]["version"] == 3


@pytest.mark.asyncio
async def test_life_chatter_selected_rolling_context_propagates_store_failure(
    tmp_path: Path,
) -> None:
    class _FailingStore:
        async def get_state(self, namespace: str, state_key: str):
            del namespace, state_key
            raise RuntimeError("remote runtime unavailable")

    service = SimpleNamespace(runtime_state_store=lambda: _FailingStore())
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config, _service=service)

    with pytest.raises(RuntimeError, match="remote runtime unavailable"):
        await chatter._load_rolling_context_snapshot(service)


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


@pytest.mark.asyncio
async def test_life_chatter_live_system_prompt_adds_broadcast_guidance(tmp_path) -> None:
    (tmp_path / "SOUL.md").write_text("SOUL_CONTENT", encoding="utf-8")
    (tmp_path / "USER.md").write_text("USER_CONTENT", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("", encoding="utf-8")

    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.plugin = SimpleNamespace(config=config)

    prompt = await chatter._build_chat_system_prompt(
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
    async def fake_system_prompt(self, *args, **kwargs):
        del self, args, kwargs
        return "test soul prompt"

    monkeypatch.setattr(LifeChatter, "_build_chat_system_prompt", fake_system_prompt)
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
    service = LifeEngineService(_service_plugin())
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
    assert "THOUGHT_STREAM_NOW" not in dynamic
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
    recorded_states: list[dict[str, object]] = []

    class ActivityService:
        async def record_conscious_activity_state(self, **kwargs) -> None:
            recorded_states.append(dict(kwargs))

        def resolve_consciousness_instance(self, stream_id: str) -> str:
            assert stream_id == "stream-a"
            return "chat-instance-a"

    async def fake_fetch_unreads():
        return [], []

    async def cancel_drive(*_args, **_kwargs):
        raise asyncio.CancelledError

    import src.core.managers.stream_manager as stream_manager_module

    monkeypatch.setattr(stream_manager_module, "get_stream_manager", lambda: DummyStreamManager())
    monkeypatch.setattr(chatter, "_get_life_service", lambda: ActivityService())
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
    assert recorded_states == [
        {
            "stream_id": "stream-a",
            "source_instance_id": "chat_global",
            "occurrence_id": "turn-1:stream-step-cancelled",
            "state_kind": "chatter_interrupted",
            "payload": {"phase": "tool_exec"},
            "surface": "life_chatter",
            "causation_id": "turn-1",
        }
    ]
    assert rt.phase == _Phase.WAIT_USER
    assert rt.active_stream_id == ""
    assert rt.active_unread_turn_key == ""

    LifeChatter.reset_global_runtime()


async def test_life_chatter_runtime_context_cursor_avoids_repeat_injection(tmp_path) -> None:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(_service_plugin(config))
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
    service = LifeEngineService(_service_plugin(config))
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
    service = LifeEngineService(_service_plugin())
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


def test_execution_tool_descriptions_respect_heartbeat_boundary() -> None:
    """执行类工具 schema 自身也要约束心跳态，不只依赖系统 prompt。"""
    bash_description = LifeEngineBashTool.tool_description
    agent_description = LifeEngineRunAgentTool.tool_description

    assert "潜意识 / 内在状态层" in bash_description
    assert "只在诊断 life_engine 自己的 workspace、日志、工具链异常时使用" in bash_description
    assert "不要用它查用户项目配置、跑用户任务、生成图片、改代码或处理外部系统" in bash_description
    assert "交给 life_chatter / 表达层" in bash_description

    assert "不是把用户请求转交后台执行的入口" in agent_description
    assert "只用于整理 life_engine 私有记忆、普通笔记" in agent_description
    assert "主动状态只能通过统一 proactive 工具读写" in agent_description
    assert "不要让子代理承接用户任务、查项目配置、跑命令、改代码、画图" in agent_description
    assert "交给 life_chatter / 表达层判断和执行" in agent_description


def test_heartbeat_prompt_uses_explicit_subject_initiative_contract(
    tmp_path,
) -> None:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(_service_plugin(config))

    prompt = "\n".join(service._build_prompt_header())

    assert "nucleus_tell_dfc" not in prompt
    assert "nucleus_proactive_command" in prompt
    assert "nucleus_proactive_query" in prompt
    assert "持续关注" in prompt
    assert "nucleus_reachability" not in prompt
    assert "nucleus_begin_outreach" not in prompt
    assert "audience_ref" in prompt
    assert "surface_ref" in prompt


def test_legacy_impulse_engine_is_not_constructed(tmp_path) -> None:
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    service = LifeEngineService(_service_plugin(config))

    assert config.drives.enabled is False
    assert not hasattr(service, "_impulse_engine")
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
