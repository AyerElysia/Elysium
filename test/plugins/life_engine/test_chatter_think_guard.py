"""life_chatter think-only 约束测试。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.core.chatter import (
    LifeChatter,
    LifeSendFileAction,
    LifeSendTextAction,
)
from plugins.life_engine.core.config import LifeEngineConfig
from plugins.life_engine.core.plugin import LifeEnginePlugin
from plugins.life_engine.core.tool_parallel import (
    is_life_tool_call_parallel_safe,
    iter_life_tool_call_batches,
)
from src.kernel.llm.exceptions import LLMContextError
from src.kernel.llm.context import LLMContextManager
from src.core.components.base.chatter import BaseChatter
from src.core.models.message import Message, MessageType
from src.kernel.llm import LLMPayload, ROLE, Text, ToolCall, ToolResult


class _FakeResponse:
    def __init__(self) -> None:
        self.payloads: list[object] = []

    def add_payload(self, payload: object) -> None:
        self.payloads.append(payload)


def _call(name: str) -> object:
    return SimpleNamespace(name=name)


def test_is_think_only_calls_true_for_single_think() -> None:
    assert LifeChatter._is_think_only_calls([_call("action-think")]) is True


def test_is_think_only_calls_false_for_mixed_actions() -> None:
    calls = [_call("action-think"), _call("action-life_send_text")]
    assert LifeChatter._is_think_only_calls(calls) is False


def test_append_think_only_retry_instruction_adds_user_payload() -> None:
    response = _FakeResponse()
    LifeChatter._append_think_only_retry_instruction(response)

    assert len(response.payloads) == 1
    payload = response.payloads[0]
    assert getattr(payload, "role", None) == ROLE.USER


def test_append_plain_text_retry_instruction_adds_user_payload() -> None:
    response = _FakeResponse()

    LifeChatter._append_plain_text_retry_instruction(
        response,
        response_text="The request was rejected because it was considered high risk",
    )

    assert len(response.payloads) == 1
    payload = response.payloads[0]
    assert getattr(payload, "role", None) == ROLE.USER
    assert payload.content[0].text
    assert "充实" in payload.content[0].text
    assert "高风险" in payload.content[0].text or "high risk" in payload.content[0].text


def test_append_plain_text_retry_instruction_uses_strict_reminder_on_last_retry() -> None:
    response = _FakeResponse()

    LifeChatter._append_plain_text_retry_instruction(
        response,
        response_text="too vague",
        retry_count=2,
    )

    payload = response.payloads[0]
    assert "最后提醒" in payload.content[0].text


def test_should_encourage_segment_send_for_long_single_content() -> None:
    call_args = {"content": "这是一条比较长的消息" * 8}
    assert LifeChatter._should_encourage_segment_send("action-life_send_text", call_args) is True


def test_should_not_encourage_segment_send_for_newline_segmented_content() -> None:
    call_args = {"content": "第一段\n第二段"}
    assert LifeChatter._should_encourage_segment_send("action-life_send_text", call_args) is False


def test_life_send_text_normalize_splits_newlines_in_plain_text() -> None:
    result = LifeSendTextAction._normalize_content_segments("第一条\n\n第二条\r\n第三条")
    assert result == ["第一条", "第二条", "第三条"]


def test_life_send_text_normalize_splits_escaped_newlines_in_string() -> None:
    result = LifeSendTextAction._normalize_content_segments("第一条\\n第二条\n第三条")
    assert result == ["第一条", "第二条", "第三条"]


def test_life_send_text_rejects_placeholder_only_content() -> None:
    action = LifeSendTextAction.__new__(LifeSendTextAction)

    ok, message = asyncio.run(action.execute("..."))

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


def test_life_send_file_action_only_registered_with_life_chatter() -> None:
    enabled_config = LifeEngineConfig()
    enabled_config.chatter.enabled = True
    enabled_components = LifeEnginePlugin(enabled_config).get_components()

    disabled_config = LifeEngineConfig()
    disabled_config.chatter.enabled = False
    disabled_components = LifeEnginePlugin(disabled_config).get_components()

    assert LifeSendFileAction.chatter_allow == ["life_chatter"]
    assert LifeSendFileAction in enabled_components
    assert LifeSendFileAction not in disabled_components


def test_append_segment_send_retry_instruction_adds_system_payload() -> None:
    response = _FakeResponse()
    LifeChatter._append_segment_send_retry_instruction(response)

    assert len(response.payloads) == 1
    payload = response.payloads[0]
    assert getattr(payload, "role", None) == ROLE.SYSTEM


def test_append_must_reply_retry_instruction_adds_user_payload() -> None:
    response = _FakeResponse()
    LifeChatter._append_must_reply_retry_instruction(response)

    assert len(response.payloads) == 1
    payload = response.payloads[0]
    assert getattr(payload, "role", None) == ROLE.USER


def test_append_inner_monologue_retry_instruction_adds_user_payload() -> None:
    response = _FakeResponse()
    LifeChatter._append_inner_monologue_retry_instruction(response)

    assert len(response.payloads) == 1
    payload = response.payloads[0]
    assert getattr(payload, "role", None) == ROLE.USER


def test_follow_up_retry_instruction_turns_assistant_tail_into_user_turn() -> None:
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

    LifeChatter._append_must_reply_retry_instruction(response)

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


def test_requires_inner_monologue_only_for_proactive_trigger_batch() -> None:
    proactive = SimpleNamespace(
        is_proactive_opportunity_trigger=True,
        is_proactive_followup_trigger=False,
    )
    followup = SimpleNamespace(
        is_proactive_opportunity_trigger=False,
        is_proactive_followup_trigger=True,
    )
    real_user = SimpleNamespace(
        is_proactive_opportunity_trigger=False,
        is_proactive_followup_trigger=False,
    )

    assert LifeChatter._requires_inner_monologue_for_unread_batch([proactive]) is True
    assert LifeChatter._requires_inner_monologue_for_unread_batch([followup]) is True
    assert LifeChatter._requires_inner_monologue_for_unread_batch([proactive, followup]) is True
    assert LifeChatter._requires_inner_monologue_for_unread_batch([real_user]) is False
    assert LifeChatter._requires_inner_monologue_for_unread_batch([proactive, real_user]) is False


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


def test_compact_successful_tool_result_only_targets_low_information_actions() -> None:
    assert LifeChatter._should_compact_successful_tool_result("action-record_inner_monologue") is True
    assert LifeChatter._should_compact_successful_tool_result("action-life_send_text") is True
    assert LifeChatter._should_compact_successful_tool_result("action-life_send_file") is True
    assert LifeChatter._should_compact_successful_tool_result("action-send_emoji_meme") is True
    assert LifeChatter._should_compact_successful_tool_result("action-think") is True
    assert LifeChatter._should_compact_successful_tool_result("search_life_memory") is False
    assert LifeChatter._should_compact_successful_tool_result("agent-life_memory_explorer") is False
    assert LifeChatter._should_compact_successful_tool_result("nucleus_search_memory") is False
    assert LifeChatter._should_compact_successful_tool_result("fetch_life_memory") is False


def test_compact_successful_tool_result_preserves_other_tool_results() -> None:
    response = _FakeResponse()
    response.add_payload(
        LLMPayload(
            ROLE.TOOL_RESULT,
            [
                ToolResult(value="已发送3条消息: hello", call_id="send-1", name="action-life_send_text"),
                ToolResult(value="记忆检索结果正文", call_id="memory-1", name="agent-life_memory_explorer"),
            ],
        )
    )

    LifeChatter._compact_successful_tool_result(response, "send-1")

    payload = response.payloads[0]
    send_result, memory_result = payload.content
    assert send_result.value == "ok"
    assert memory_result.value == "记忆检索结果正文"


def test_life_tool_parallel_policy_only_allows_safe_reads() -> None:
    assert is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_read_file", args={})
    )
    assert is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_manage_thought_stream", args={"action": "list"})
    )
    assert not is_life_tool_call_parallel_safe(
        SimpleNamespace(name="nucleus_manage_thought_stream", args={"action": "advance"})
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


@pytest.mark.asyncio
async def test_life_chatter_run_tool_call_accepts_single_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
    assert payload.content[0].value == "ok"


@pytest.mark.asyncio
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
