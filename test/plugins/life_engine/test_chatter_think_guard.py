"""life_chatter default-loop 行为与工具执行测试。"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.life_engine.core.chatter import (
    LifeChatter,
    LifeRecognizeVoiceTool,
    LifeSaveMediaTool,
    LifeSendFileAction,
    LifeSendImageAction,
    LifeSendTextAction,
    LifeSendVoiceAction,
)
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.plugin import LifeEnginePlugin
from plugins.life_engine.core.tool_parallel import (
    is_life_tool_call_parallel_safe,
    iter_life_tool_call_batches,
)
from src.core.components.base.chatter import BaseChatter
from src.core.models.message import Message, MessageType
from src.kernel.llm import ROLE, LLMPayload, Text, ToolCall, ToolResult
from src.kernel.llm.context import LLMContextManager
from src.kernel.llm.exceptions import LLMContextError


class _FakeResponse:
    def __init__(self) -> None:
        self.payloads: list[object] = []

    def add_payload(self, payload: object) -> None:
        self.payloads.append(payload)


def test_ensure_unique_tool_call_ids_rewrites_duplicates() -> None:
    calls = [
        ToolCall(id="tooluse_same", name="action-think", args={}),
        ToolCall(id="tooluse_same", name="action-life_send_text", args={"content": "hi"}),
        ToolCall(id=None, name="action-life_send_text", args={"content": "again"}),
    ]

    LifeChatter._ensure_unique_tool_call_ids(calls)

    ids = [call.id for call in calls]
    assert ids[0] == "tooluse_same"
    assert len(set(ids)) == len(ids)
    assert ids[1] != "tooluse_same"
    assert ids[2]

    manager = LLMContextManager()
    payloads = manager.add_payload([], LLMPayload(ROLE.USER, Text("run tools")))
    payloads = manager.add_payload(payloads, LLMPayload(ROLE.ASSISTANT, calls))
    for call in calls:
        payloads = manager.add_payload(
            payloads,
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(value="ok", call_id=call.id, name=call.name),
            ),
        )
    manager.validate_for_send(payloads)


async def test_conscious_tool_activity_records_same_call_arguments_and_result() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    call = ToolCall(
        id="send-activity-1",
        name="action-life_send_text",
        args={
            "mood": "温柔",
            "decision": "直接回应",
            "expected_response": "对方感到被看见",
            "thought": "我会认真接住这句话。",
            "content": "我在听。",
        },
    )
    service = SimpleNamespace(
        record_conscious_model_turn=AsyncMock(
            return_value={"send-activity-1": "activity-1"}
        ),
        record_conscious_tool_results=AsyncMock(),
    )

    activity_ids = await chatter._record_conscious_tool_calls(
        service,
        [call],
        SimpleNamespace(
            request_record_id="request-1",
            reasoning_content="provider 内部推理",
            message="同轮独白",
        ),
        stream_id="stream-1",
        turn_occurrence_id="turn-1",
        source_instance_id="chat-instance-1",
        fallback_model_turn_id="fallback-1",
    )
    response = SimpleNamespace(
        payloads=[
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(
                    value={"status": "delivered"},
                    call_id="send-activity-1",
                    name="action-life_send_text",
                ),
            )
        ]
    )
    await chatter._record_conscious_tool_results(
        service,
        [call],
        response,
        stream_id="stream-1",
        turn_occurrence_id="turn-1",
        source_instance_id="chat-instance-1",
        activity_ids=activity_ids,
        outcomes={
            "send-activity-1": {
                "success": True,
                "technical_outcome": "delivered",
                "delivery_receipt_sha256": "a" * 64,
                "delivery_message_id": "provider-1",
                "delivery_proof_status": "durable",
            }
        },
    )

    call_payload = service.record_conscious_model_turn.await_args.kwargs
    assert call_payload["source_instance_id"] == "chat-instance-1"
    assert call_payload["turn_occurrence_id"] == "turn-1"
    assert call_payload["transport_request_id"] == "request-1"
    assert call_payload["provider_reasoning_content"] == "provider 内部推理"
    assert call_payload["assistant_message"] == "同轮独白"
    assert call_payload["calls"][0]["arguments"] == call.args
    result_payload = service.record_conscious_tool_results.await_args.kwargs
    assert result_payload["activity_ids"] == {
        "send-activity-1": "activity-1"
    }
    assert result_payload["results"][0]["result"] == {
        "status": "delivered"
    }
    assert result_payload["results"][0]["success"] is True
    assert result_payload["results"][0]["technical_outcome"] == "delivered"


async def test_empty_model_turn_is_still_recorded_as_conscious_activity() -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    service = SimpleNamespace(
        record_conscious_model_turn=AsyncMock(return_value={}),
    )
    response = SimpleNamespace(
        request_record_id="request-empty",
        reasoning_content="",
        message="",
    )

    activity_ids = await chatter._record_conscious_tool_calls(
        service,
        [],
        response,
        stream_id="stream-empty",
        turn_occurrence_id="turn-empty",
        source_instance_id="chat-instance-empty",
        fallback_model_turn_id="fallback-empty",
    )

    assert activity_ids == {}
    payload = service.record_conscious_model_turn.await_args.kwargs
    assert payload["transport_request_id"] == "request-empty"
    assert payload["provider_reasoning_content"] == ""
    assert payload["assistant_message"] == ""
    assert payload["calls"] == []


def test_life_decision_panel_maps_reasoning_message_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = [
        ToolCall(
            id="send-1",
            name="action-life_send_text",
            args={"content": "你好", "reason": "内部理由"},
        )
    ]
    response = SimpleNamespace(
        reasoning_content="先确认对方是不是在问新逻辑。",
        message="我感觉到一点变化。",
        call_list=calls,
    )
    chat_stream = SimpleNamespace(stream_name="始源之地", stream_id="stream-1")
    panels: list[tuple[str, str, str]] = []

    monkeypatch.setattr(
        "plugins.life_engine.core.chatter.logger",
        SimpleNamespace(
            print_panel=lambda content, title, border_style: panels.append(
                (content, title, border_style)
            )
        ),
    )

    LifeChatter._print_life_decision_panel(chat_stream, response)

    assert panels
    content, title, border_style = panels[0]
    assert title == "Life Chatter 决策"
    assert border_style == "magenta"
    assert "聊天流名称：始源之地" in content
    assert "思考：先确认对方是不是在问新逻辑。" in content
    assert "独白：我感觉到一点变化。" in content
    assert "action-life_send_text (content: 你好)" in content
    assert "内部理由" not in content


def test_life_send_text_normalize_preserves_newlines_in_one_message() -> None:
    result = LifeSendTextAction._normalize_content_segments("第一条\n\n第二条\r\n第三条")
    assert result == ["第一条\n\n第二条\n第三条"]


def test_life_send_text_normalize_preserves_escaped_newlines_in_one_message() -> None:
    result = LifeSendTextAction._normalize_content_segments("第一条\\n第二条\n第三条")
    assert result == ["第一条\n第二条\n第三条"]


def test_life_send_text_rejects_placeholder_only_content() -> None:
    action = LifeSendTextAction.__new__(LifeSendTextAction)
    ok, message = asyncio.run(action.execute("...", thought="å›žåº”å½“å‰æ¶ˆæ¯ã€‚"))
    assert ok is False
    assert "占位符" in message


class _FakeAdapterManager:
    async def get_bot_info_by_platform(self, _platform: str) -> dict[str, str]:
        return {"bot_id": "bot-1", "bot_name": "Elysia"}


class _FakeStreamManager:
    async def get_stream_info(self, _stream_id: str) -> None:
        return None


class _FakeMessageSender:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.messages: list[Message] = []

    async def send_message(self, message: Message) -> bool:
        self.messages.append(message)
        return self.success


def _make_file_action(chat_type: str, last_message: Message) -> LifeSendFileAction:
    context = SimpleNamespace(
        unread_messages=[last_message],
        history_messages=[],
        current_message=last_message,
        message_cache=[],
        triggering_user_id="",
    )
    action = LifeSendFileAction.__new__(LifeSendFileAction)
    action.chat_stream = SimpleNamespace(
        stream_id=f"stream-{chat_type}",
        platform="qq",
        chat_type=chat_type,
        context=context,
    )
    action.plugin = SimpleNamespace()
    return action


def test_life_send_file_rejects_invalid_paths(tmp_path: Path) -> None:
    action = LifeSendFileAction.__new__(LifeSendFileAction)
    missing = tmp_path / "missing.txt"

    ok_relative, message_relative = asyncio.run(action.execute("relative.txt"))
    ok_missing, message_missing = asyncio.run(action.execute(str(missing)))
    ok_dir, message_dir = asyncio.run(action.execute(str(tmp_path)))
    ok_glob, message_glob = asyncio.run(action.execute(str(tmp_path / "*.txt")))

    assert ok_relative is False
    assert "绝对路径" in message_relative
    assert ok_missing is False
    assert "不存在" in message_missing
    assert ok_dir is False
    assert "普通文件" in message_dir
    assert ok_glob is False
    assert "通配符" in message_glob


def test_life_send_file_rejects_oversized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    file_path = tmp_path / "large.bin"
    file_path.write_bytes(b"xx")
    action = LifeSendFileAction.__new__(LifeSendFileAction)
    monkeypatch.setattr(LifeSendFileAction, "MAX_FILE_BYTES", 1)

    ok, message = asyncio.run(action.execute(str(file_path)))
    assert ok is False
    assert "文件过大" in message


def test_life_send_file_sends_private_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "note.txt"
    file_path.write_text("hello", encoding="utf-8")
    last_message = Message(
        message_id="m1",
        content="send it",
        sender_id="user-1",
        sender_name="Ayer",
        platform="qq",
        chat_type="private",
        stream_id="stream-private",
    )
    sender = _FakeMessageSender()
    action = _make_file_action("private", last_message)

    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: _FakeAdapterManager(),
    )
    monkeypatch.setattr(
        "src.core.managers.stream_manager.get_stream_manager",
        lambda: _FakeStreamManager(),
    )
    monkeypatch.setattr("src.core.transport.message_send.get_message_sender", lambda: sender)

    ok, message = asyncio.run(action.execute(str(file_path)))

    assert ok is True
    assert "已发送文件" in message
    assert len(sender.messages) == 1
    sent = sender.messages[0]
    assert sent.message_type == MessageType.FILE
    assert sent.content == {"path": str(file_path.resolve())}
    assert sent.processed_plain_text == "[发送文件] note.txt"
    assert sent.stream_id == "stream-private"
    assert sent.extra["target_user_id"] == "user-1"
    assert sent.extra["target_user_name"] == "Ayer"
    assert sent.extra["file_name"] == "note.txt"


def test_life_send_file_sends_group_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    file_path = tmp_path / "report.pdf"
    file_path.write_bytes(b"%PDF")
    last_message = Message(
        message_id="m1",
        content="send it",
        sender_id="user-1",
        sender_name="Ayer",
        platform="qq",
        chat_type="group",
        stream_id="stream-group",
        group_id="12345",
        group_name="群聊",
    )
    sender = _FakeMessageSender()
    action = _make_file_action("group", last_message)

    monkeypatch.setattr(
        "src.core.managers.adapter_manager.get_adapter_manager",
        lambda: _FakeAdapterManager(),
    )
    monkeypatch.setattr("src.core.transport.message_send.get_message_sender", lambda: sender)

    ok, message = asyncio.run(action.execute(str(file_path)))

    assert ok is True
    assert "已发送文件" in message
    sent = sender.messages[0]
    assert sent.message_type == MessageType.FILE
    assert sent.extra["target_group_id"] == "12345"
    assert sent.extra["target_group_name"] == "群聊"


async def test_life_send_image_action_sends_existing_local_image(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "sample.png"
    image_path.write_bytes(b"fake-png")
    sent: dict[str, object] = {}

    async def fake_send_image(image_data, stream_id, **kwargs):
        sent.update({"image_data": image_data, "stream_id": stream_id, **kwargs})
        return True

    monkeypatch.setattr(
        "src.app.plugin_system.api.send_api.send_image",
        fake_send_image,
    )
    action = LifeSendImageAction.__new__(LifeSendImageAction)
    action.chat_stream = SimpleNamespace(stream_id="stream-1", platform="feishu")

    ok, message = await action.execute(str(image_path))

    assert ok is True
    assert "已发送图片" in message
    assert sent["stream_id"] == "stream-1"
    assert sent["platform"] == "feishu"
    assert sent["image_data"] == "ZmFrZS1wbmc="


async def test_life_send_voice_action_sends_existing_local_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_path = tmp_path / "sample.wav"
    voice_path.write_bytes(b"fake-wav")
    sent: dict[str, object] = {}

    async def fake_send_voice(voice_data, stream_id, **kwargs):
        sent.update({"voice_data": voice_data, "stream_id": stream_id, **kwargs})
        return True

    monkeypatch.setattr(
        "src.app.plugin_system.api.send_api.send_voice",
        fake_send_voice,
    )
    action = LifeSendVoiceAction.__new__(LifeSendVoiceAction)
    action.chat_stream = SimpleNamespace(stream_id="stream-1", platform="feishu")

    ok, message = await action.execute(str(voice_path))

    assert ok is True
    assert "已发送语音" in message
    assert sent["stream_id"] == "stream-1"
    assert sent["voice_data"] == "ZmFrZS13YXY="


async def test_life_save_media_tool_writes_image_inside_workspace(
    tmp_path: Path,
) -> None:
    image_message = Message(
        message_id="image-1",
        content="ZmFrZS1pbWFnZQ==",
        processed_plain_text="[图片]",
        message_type=MessageType.IMAGE,
        sender_id="user-1",
        sender_name="Ayer",
        platform="feishu",
        chat_type="private",
        stream_id="stream-1",
    )
    config = LifeEngineConfig()
    config.settings.workspace_path = str(tmp_path)
    tool = LifeSaveMediaTool.__new__(LifeSaveMediaTool)
    tool.plugin = SimpleNamespace(config=config)
    tool.chat_stream = SimpleNamespace(
        context=SimpleNamespace(
            unread_messages=[image_message],
            current_message=image_message,
            history_messages=[],
        )
    )

    ok, result = await tool.execute("latest", "image", "received/sample.png")

    assert ok is True
    assert isinstance(result, dict)
    saved_path = Path(result["saved_to"])
    assert saved_path == tmp_path / "received" / "sample.png"
    assert saved_path.read_bytes() == b"fake-image"


async def test_life_save_media_tool_copies_received_file_without_body_in_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received_root = tmp_path / "data" / "received_files" / "qq"
    received_root.mkdir(parents=True)
    source = received_root / "story.md"
    body = "故事的真实进度".encode()
    source.write_bytes(body)
    workspace = tmp_path / "workspace"
    file_message = Message(
        message_id="file-1",
        content={
            "media": [
                {
                    "type": "file",
                    "data": {
                        "name": "story.md",
                        "storage_key": "qq/story.md",
                        "size": len(body),
                        "materialized": True,
                    },
                }
            ]
        },
        processed_plain_text="[文件:story.md]",
        message_type=MessageType.FILE,
        sender_id="user-1",
        sender_name="Ayer",
        platform="qq",
        chat_type="private",
        stream_id="stream-1",
    )
    config = LifeEngineConfig()
    config.settings.workspace_path = str(workspace)
    tool = LifeSaveMediaTool.__new__(LifeSaveMediaTool)
    tool.plugin = SimpleNamespace(config=config)
    tool.chat_stream = SimpleNamespace(
        context=SimpleNamespace(
            unread_messages=[file_message],
            current_message=file_message,
            history_messages=[],
        )
    )
    monkeypatch.chdir(tmp_path)

    ok, result = await tool.execute("latest", "file", "received/story.md")

    assert ok is True
    assert isinstance(result, dict)
    saved_path = Path(result["saved_to"])
    assert saved_path == workspace / "received" / "story.md"
    assert saved_path.read_bytes() == body
    assert source.read_bytes() == body
    assert result["kind"] == "file"
    assert result["size_bytes"] == len(body)
    assert result["sha256"] == hashlib.sha256(body).hexdigest()
    assert body.decode() not in str(result)


async def test_life_recognize_voice_tool_uses_current_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    voice_message = Message(
        message_id="voice-1",
        content="ZmFrZS1hdWRpbw==",
        processed_plain_text="[语音]",
        message_type=MessageType.VOICE,
        sender_id="user-1",
        sender_name="Ayer",
        platform="feishu",
        chat_type="private",
        stream_id="stream-1",
    )
    tool = LifeRecognizeVoiceTool.__new__(LifeRecognizeVoiceTool)
    tool.chat_stream = SimpleNamespace(
        context=SimpleNamespace(
            unread_messages=[voice_message],
            current_message=voice_message,
            history_messages=[],
        )
    )
    manager = SimpleNamespace(
        recognize_voice=AsyncMock(return_value="语音转写：你好"),
    )
    monkeypatch.setattr(
        "src.core.managers.media_manager.get_media_manager",
        lambda: manager,
    )

    ok, result = await tool.execute("latest")

    assert ok is True
    assert result == "语音转写：你好"
    manager.recognize_voice.assert_awaited_once()


async def test_life_send_voice_action_uses_local_tts_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """text 模式应调用正式本地 TTS Service，并原样发送 Base64 音频。"""
    action = LifeSendVoiceAction.__new__(LifeSendVoiceAction)
    action.chat_stream = SimpleNamespace(stream_id="stream-1", platform="feishu")
    service = SimpleNamespace(
        generate_voice=AsyncMock(return_value="LOCAL_AUDIO_BASE64"),
    )
    resolved: list[str] = []
    sent: dict[str, object] = {}

    async def fake_send_voice(voice_data, stream_id, **kwargs):
        sent.update({"voice_data": voice_data, "stream_id": stream_id, **kwargs})
        return True

    monkeypatch.setattr(
        "plugins.tts_voice_plugin.api.get_local_tts_service",
        lambda: resolved.append("tts_voice_plugin:service:tts") or service,
    )
    monkeypatch.setattr(
        "src.app.plugin_system.api.send_api.send_voice",
        fake_send_voice,
    )

    ok, result = await action.execute(
        text="晚上好。",
        voice_style="gentle",
        text_language="zh",
    )

    assert ok is True
    assert "已合成并发送语音" in result
    assert resolved == ["tts_voice_plugin:service:tts"]
    service.generate_voice.assert_awaited_once_with(
        text="晚上好。",
        style_hint="gentle",
        language_hint="zh",
    )
    assert sent["voice_data"] == "LOCAL_AUDIO_BASE64"
    assert sent["stream_id"] == "stream-1"
    assert sent["platform"] == "feishu"
    assert sent["processed_plain_text"] == "[语音:晚上好。]"


async def test_life_send_voice_action_fails_closed_without_local_tts_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = LifeSendVoiceAction.__new__(LifeSendVoiceAction)
    action.chat_stream = SimpleNamespace(stream_id="stream-1", platform="feishu")
    monkeypatch.setattr(
        "plugins.tts_voice_plugin.api.get_local_tts_service",
        lambda: None,
    )

    ok, result = await action.execute(text="晚上好。")

    assert ok is False
    assert result == "本地消息 TTS 服务未启用或尚未就绪"


async def test_life_send_voice_action_does_not_duplicate_surface_auto_tts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = LifeSendVoiceAction.__new__(LifeSendVoiceAction)
    action.chat_stream = SimpleNamespace(
        stream_id="surface-stream",
        platform="neko.surface",
    )

    def fail_if_resolved(_signature: str) -> None:
        raise AssertionError("Surface text mode must not resolve message TTS")

    monkeypatch.setattr(
        "plugins.tts_voice_plugin.api.get_local_tts_service",
        lambda: fail_if_resolved("tts_voice_plugin:service:tts"),
    )

    ok, result = await action.execute(text="这句话只应由 Surface 自动朗读。")

    assert ok is False
    assert result == "N.E.K.O Surface 已由本地自动语音链接管"


async def test_life_send_voice_action_rejects_empty_local_tts_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    action = LifeSendVoiceAction.__new__(LifeSendVoiceAction)
    action.chat_stream = SimpleNamespace(stream_id="stream-1", platform="feishu")
    service = SimpleNamespace(generate_voice=AsyncMock(return_value=""))
    monkeypatch.setattr(
        "plugins.tts_voice_plugin.api.get_local_tts_service",
        lambda: service,
    )

    ok, result = await action.execute(text="不会被伪装成成功。")

    assert ok is False
    assert result == "本地消息 TTS 未返回音频"


def test_life_send_voice_schema_contains_only_local_tts_parameters() -> None:
    schema = LifeSendVoiceAction.to_schema()["function"]
    properties = schema["parameters"]["properties"]

    assert set(properties) == {"path", "text", "voice_style", "text_language"}
    assert "voice" not in properties
    assert "instructions" not in properties
    assert "model_tasks.tts" not in schema["description"]
    assert "MiMo" not in schema["description"]
    assert "当前私聊或群聊" in schema["description"]
    assert "send_group_ai_record" in schema["description"]
    assert "QQ 内置角色语音" in schema["description"]


def test_life_media_capabilities_are_registered_for_enabled_chatter() -> None:
    """附件能力只在 LifeChatter 启用时注册，并保持主体主动调用。"""
    enabled_config = LifeEngineConfig()
    enabled_config.chatter.enabled = True
    enabled_components = LifeEnginePlugin(enabled_config).get_components()

    disabled_config = LifeEngineConfig()
    disabled_config.chatter.enabled = False
    disabled_components = LifeEnginePlugin(disabled_config).get_components()

    media_components = {
        LifeSendFileAction,
        LifeSendImageAction,
        LifeSendVoiceAction,
        LifeRecognizeVoiceTool,
        LifeSaveMediaTool,
    }
    assert media_components <= set(enabled_components)
    assert media_components.isdisjoint(disabled_components)
    assert all(component.chatter_allow == ["life_chatter"] for component in media_components)


def test_life_send_file_action_is_registered_only_for_enabled_chatter() -> None:
    """普通文件是正式主体能力，但不会在 Chatter 关闭时泄漏注册。"""
    enabled_config = LifeEngineConfig()
    enabled_config.chatter.enabled = True
    enabled_components = LifeEnginePlugin(enabled_config).get_components()

    disabled_config = LifeEngineConfig()
    disabled_config.chatter.enabled = False
    disabled_components = LifeEnginePlugin(disabled_config).get_components()

    assert LifeSendFileAction.chatter_allow == ["life_chatter"]
    assert LifeSendFileAction in enabled_components
    assert LifeSendFileAction not in disabled_components


def test_follow_up_instruction_turns_assistant_tail_into_user_turn() -> None:
    """_append_follow_up_user_instruction 应把 TOOL_RESULT 尾部补成合法 USER 轮。"""
    response = _FakeResponse()
    response.add_payload(LLMPayload(ROLE.USER, Text("上一轮用户消息")))
    response.add_payload(
        LLMPayload(
            ROLE.ASSISTANT,
            [Text("需要回复"), ToolCall(id="call-1", name="tool-inspect_media", args="{}")],
        )
    )
    response.add_payload(
        LLMPayload(
            ROLE.TOOL_RESULT,
            ToolResult(value="ok", call_id="call-1", name="tool-inspect_media"),
        )
    )
    response.add_payload(LLMPayload(ROLE.ASSISTANT, Text("__SUSPEND__")))

    LifeChatter._append_follow_up_user_instruction(response, "继续")

    assert [payload.role for payload in response.payloads] == [
        ROLE.USER,
        ROLE.ASSISTANT,
        ROLE.TOOL_RESULT,
        ROLE.ASSISTANT,
        ROLE.USER,
    ]

    manager = LLMContextManager()
    validated = list(response.payloads)
    manager._validate_payloads(validated, allow_incomplete_tail=True)

    with pytest.raises(LLMContextError):
        manager._validate_payloads(
            [
                LLMPayload(ROLE.USER, Text("上一轮用户消息")),
                LLMPayload(ROLE.ASSISTANT, Text("上一轮助手消息")),
                LLMPayload(ROLE.ASSISTANT, Text("非法的连续 assistant")),
            ],
            allow_incomplete_tail=True,
        )


def test_visible_reply_action_accepts_emoji_send() -> None:
    assert LifeChatter._is_visible_reply_action("action-life_send_text") is True
    assert LifeChatter._is_visible_reply_action("action-life_send_file") is True
    assert LifeChatter._is_visible_reply_action("action-send_emoji_meme") is True
    assert LifeChatter._is_visible_reply_action("action-schedule_followup_message") is False


def test_should_force_reply_only_for_real_external_messages() -> None:
    proactive = SimpleNamespace(
        is_proactive_opportunity_trigger=True,
        is_proactive_followup_trigger=False,
        sender_role="other",
    )
    real_user = SimpleNamespace(
        is_proactive_opportunity_trigger=False,
        is_proactive_followup_trigger=False,
        sender_role="other",
    )

    assert LifeChatter._should_force_reply_for_unread_batch([proactive]) is False
    assert LifeChatter._should_force_reply_for_unread_batch([real_user]) is True


def test_should_force_reply_for_decision_when_response_is_accepted() -> None:
    proactive = SimpleNamespace(
        is_proactive_opportunity_trigger=True,
        is_proactive_followup_trigger=False,
        sender_role="other",
    )
    real_user = SimpleNamespace(
        is_proactive_opportunity_trigger=False,
        is_proactive_followup_trigger=False,
        sender_role="other",
    )

    assert LifeChatter._should_force_reply_for_decision(
        {"should_respond": True},
        [real_user],
    ) is True
    assert LifeChatter._should_force_reply_for_decision(
        {"should_respond": True, "force_reply": False},
        [real_user],
    ) is False
    assert LifeChatter._should_force_reply_for_decision(
        {"should_respond": True, "force_reply": True},
        [real_user],
    ) is True
    assert LifeChatter._should_force_reply_for_decision(
        {"should_respond": True, "force_reply": True},
        [proactive],
    ) is False


def test_life_tool_parallel_policy_only_allows_safe_reads() -> None:
    assert is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_read_file", args={})
    )
    assert not is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_manage_thought_stream", args={"action": "list"})
    )
    assert is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_proactive_query", args={"resource": "attention"})
    )
    assert not is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_manage_attention_thread", args={"action": "list"})
    )
    assert not is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_manage_thought_stream", args={"action": "advance"})
    )
    assert not is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_manage_attention_thread", args={"action": "note"})
    )
    assert not is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_search_memory", args={"query": "x"})
    )
    assert not is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_write_file", args={})
    )
    assert not is_life_tool_call_parallel_safe(
        SimpleNamespace(name="action-life_send_text", args={})
    )


def test_life_tool_parallel_batches_only_consecutive_safe_calls() -> None:
    calls = [
        SimpleNamespace(name="nucleus_read_file", args={}),
        SimpleNamespace(name="nucleus_web_search", args={}),
        SimpleNamespace(name="nucleus_write_file", args={}),
        SimpleNamespace(name="nucleus_list_files", args={}),
        SimpleNamespace(name="action-life_send_text", args={}),
    ]

    batches = [
        ([call.name for call in batch], can_parallel)
        for batch, can_parallel in iter_life_tool_call_batches(calls)
    ]

    assert batches == [
        (["nucleus_read_file", "nucleus_web_search"], True),
        (["nucleus_write_file"], False),
        (["nucleus_list_files"], True),
        (["action-life_send_text"], False),
    ]


def test_life_chatter_blocks_live_bridge_tool_at_execution_time() -> None:
    class FakeTTSAction:
        @classmethod
        def get_signature(cls) -> str:
            return "tts_voice_plugin:action:tts_voice_action"

    usable_map = {"action-tts_voice_action": FakeTTSAction}
    call = SimpleNamespace(name="action-tts_voice_action")
    live_msg = SimpleNamespace(platform="live")
    qq_msg = SimpleNamespace(platform="qq")

    assert LifeChatter._is_tool_call_blocked_for_trigger(call, usable_map, live_msg)
    assert not LifeChatter._is_tool_call_blocked_for_trigger(call, usable_map, qq_msg)


async def test_life_chatter_run_tool_call_accepts_single_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单调用结果不再被压缩；值保持原样。"""
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.stream_id = "life-test-stream"
    response = _FakeResponse()
    response.add_payload(
        LLMPayload(
            ROLE.TOOL_RESULT,
            [ToolResult(value="已发送", call_id="send-1", name="action-life_send_text")],
        )
    )
    captured: dict[str, object] = {}
    call = SimpleNamespace(id="send-1", name="action-life_send_text", args={})

    async def _fake_base_run_tool_call(
        self: BaseChatter,
        calls: object,
        _response: object,
        _usable_map: object,
        _trigger_msg: object,
    ) -> list[tuple[bool, bool]]:
        captured["calls"] = calls
        return [(True, True)]

    monkeypatch.setattr(BaseChatter, "run_tool_call", _fake_base_run_tool_call)

    appended, success = await chatter.run_tool_call(
        call,
        response,
        usable_map={},
        trigger_msg=None,
    )

    assert (appended, success) == (True, True)
    assert captured["calls"] == [call]
    payload = response.payloads[0]
    assert payload.content[0].value == "已发送"


async def test_life_chatter_run_tool_call_preserves_batch_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chatter = LifeChatter.__new__(LifeChatter)
    chatter.stream_id = "life-test-stream"
    response = _FakeResponse()
    calls = [
        SimpleNamespace(id="send-1", name="action-life_send_text", args={}),
        SimpleNamespace(id="memory-1", name="search_life_memory", args={}),
    ]
    captured: dict[str, object] = {}

    async def _fake_base_run_tool_call(
        self: BaseChatter,
        incoming_calls: object,
        _response: object,
        _usable_map: object,
        _trigger_msg: object,
    ) -> list[tuple[bool, bool]]:
        captured["calls"] = incoming_calls
        return [(True, True), (True, True)]

    monkeypatch.setattr(BaseChatter, "run_tool_call", _fake_base_run_tool_call)

    results = await chatter.run_tool_call(
        calls,
        response,
        usable_map={},
        trigger_msg=None,
    )

    assert results == [(True, True), (True, True)]
    assert captured["calls"] == calls
