"""OpenAI客户端测试。

使用mock来模拟OpenAI SDK，避免依赖真实的API调用。
"""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from typing import cast, Any

import pytest

from src.kernel.llm import (
    Audio,
    Image,
    LLMPayload,
    MediaValidationError,
    UnsupportedModalityError,
    ReasoningText,
    ROLE,
    Text,
    ToolCall,
    ToolResult,
    Video,
)


_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
_WEBP_BYTES = b"RIFF\x10\x00\x00\x00WEBPVP8 "
_MP3_BYTES = b"ID3\x04\x00\x00"
_WAV_BYTES = b"RIFF$\x00\x00\x00WAVEfmt "
_MP4_BYTES = b"\x00\x00\x00\x18ftypmp42"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class TestImageToDataUrl:
    """测试 OpenAI 只消费已校验的 Image IR。"""

    def test_uses_validated_image_mime_and_bytes(self):
        from src.kernel.llm.model_client.openai_client import _image_to_data_url

        image = Image.from_bytes(_PNG_BYTES)
        assert _image_to_data_url(image) == image.data_url

    def test_uses_detected_webp_mime(self):
        from src.kernel.llm.model_client.openai_client import _image_to_data_url

        image = Image.from_bytes(_WEBP_BYTES)
        assert _image_to_data_url(image) == image.data_url
        assert _image_to_data_url(image).startswith("data:image/webp;base64,")

    def test_does_not_accept_unvalidated_strings(self):
        from src.kernel.llm.model_client.openai_client import _image_to_data_url

        with pytest.raises(AttributeError):
            _image_to_data_url("data:image/png;base64,anything")  # type: ignore[arg-type]


class TestPayloadsToOpenAIMessages:
    """测试_payloads_to_openai_messages函数。"""

    def test_user_text_message(self):
        """测试用户文本消息。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [LLMPayload(ROLE.USER, Text("Hello"))]
        messages, tools = _payloads_to_openai_messages(payloads)

        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "Hello"
        assert len(tools) == 0

    def test_assistant_message(self):
        """测试助手消息。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [LLMPayload(ROLE.ASSISTANT, Text("Hi there"))]
        messages, tools = _payloads_to_openai_messages(payloads)

        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "Hi there"

    def test_assistant_tool_call_string_args_are_normalized_to_json(self):
        """测试 assistant.tool_calls 的字符串参数会被规范化为合法 JSON。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.ASSISTANT,
                [
                    Text("I'll call a tool"),
                    ToolCall(id="call_1", name="calculator", args="{'a': 1, 'b': 'x'}"),
                ],
            )
        ]

        messages, tools = _payloads_to_openai_messages(payloads)

        assert tools == []
        assert len(messages) == 1
        tool_calls = messages[0]["tool_calls"]
        assert len(tool_calls) == 1
        assert json.loads(tool_calls[0]["function"]["arguments"]) == {"a": 1, "b": "x"}

    def test_assistant_tool_call_empty_string_args_become_empty_object(self):
        """测试 assistant.tool_calls 的空字符串参数会回退为空对象。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.ASSISTANT,
                [ToolCall(id="call_1", name="calculator", args="  ")],
            )
        ]

        messages, tools = _payloads_to_openai_messages(payloads)

        assert tools == []
        assert len(messages) == 1
        tool_calls = messages[0]["tool_calls"]
        assert len(tool_calls) == 1
        assert json.loads(tool_calls[0]["function"]["arguments"]) == {}

    def test_tool_result_name_is_omitted_by_default(self):
        """默认保持标准 OpenAI tool message 结构，不额外加入 name。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.ASSISTANT,
                [ToolCall(id="call_1", name="calculator", args={})],
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                [ToolResult(value="42", call_id="call_1", name="calculator")],
            ),
        ]

        messages, tools = _payloads_to_openai_messages(payloads)

        assert tools == []
        assert messages[1] == {
            "role": "tool",
            "content": "42",
            "tool_call_id": "call_1",
        }

    def test_tool_result_name_can_be_backfilled_for_gemini_compat(self):
        """Gemini 兼容链路需要 tool message name 来生成 function_response.name。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.ASSISTANT,
                [ToolCall(id="call_1", name="calculator", args={})],
            ),
            LLMPayload(
                ROLE.TOOL_RESULT,
                [ToolResult(value="42", call_id="call_1")],
            ),
        ]

        messages, tools = _payloads_to_openai_messages(
            payloads,
            include_tool_result_name=True,
        )

        assert tools == []
        assert messages[1] == {
            "role": "tool",
            "content": "42",
            "tool_call_id": "call_1",
            "name": "calculator",
        }

    def test_plain_text_tool_result_is_repaired_for_gemini_compat(self):
        """旧上下文中的纯文本工具结果也应恢复 call id 和工具名。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.ASSISTANT,
                [ToolCall(id="call_1", name="calculator", args={})],
            ),
            LLMPayload(ROLE.TOOL_RESULT, [Text("42")]),
        ]

        messages, tools = _payloads_to_openai_messages(
            payloads,
            include_tool_result_name=True,
        )

        assert tools == []
        assert messages[1] == {
            "role": "tool",
            "content": "42",
            "tool_call_id": "call_1",
            "name": "calculator",
        }

    def test_plain_text_tool_results_follow_pending_call_order_for_gemini(self):
        """多个旧式工具结果按 assistant 声明的调用顺序配对。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.ASSISTANT,
                [
                    ToolCall(id="call_1", name="first_tool", args={}),
                    ToolCall(id="call_2", name="second_tool", args={}),
                ],
            ),
            LLMPayload(ROLE.TOOL_RESULT, [Text("first")]),
            LLMPayload(ROLE.TOOL_RESULT, [Text("second")]),
        ]

        messages, _ = _payloads_to_openai_messages(
            payloads,
            include_tool_result_name=True,
        )

        assert messages[1]["tool_call_id"] == "call_1"
        assert messages[1]["name"] == "first_tool"
        assert messages[2]["tool_call_id"] == "call_2"
        assert messages[2]["name"] == "second_tool"

    def test_orphan_plain_text_tool_result_becomes_context_for_gemini(self):
        """无法配对的旧工具结果不能生成 Gemini 非法 function_response。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        messages, _ = _payloads_to_openai_messages(
            [LLMPayload(ROLE.TOOL_RESULT, [Text("legacy result")])],
            include_tool_result_name=True,
        )

        assert messages == [{"role": "user", "content": "legacy result"}]

    def test_parse_completion_message_repairs_non_json_tool_arguments(self):
        """测试响应解析会修复单引号形式的 tool arguments。"""
        from src.kernel.llm.model_client.openai_client import _parse_completion_message

        mock_message = MagicMock()
        mock_tool_call = MagicMock()
        mock_tool_call.id = "call_1"
        mock_tool_call.function.name = "calculator"
        mock_tool_call.function.arguments = "{'a': 1, 'b': 'x'}"
        mock_message.content = ""
        mock_message.tool_calls = [mock_tool_call]
        mock_message.function_call = None

        message, tool_calls, reasoning_content = _parse_completion_message(mock_message)

        assert message == ""
        assert tool_calls == [{"id": "call_1", "name": "calculator", "args": {"a": 1, "b": "x"}}]
        assert reasoning_content is None

    def test_extract_reasoning_content_from_dict_message(self):
        """测试 dict 形态 provider 响应中的 reasoning_content 会被提取。"""
        from src.kernel.llm.model_client.openai_client import _extract_reasoning_content

        message = {"reasoning_content": [{"text": "先判断。"}, {"content": "再调用工具。"}]}

        assert _extract_reasoning_content(message) == "先判断。再调用工具。"

    def test_multimodal_content(self):
        """测试多模态内容（文本+图片）。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.USER,
                [Text("What's in this image?"), Image.from_bytes(_PNG_BYTES)],
            )
        ]
        messages, tools = _payloads_to_openai_messages(payloads)

        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        content = messages[0]["content"]
        assert isinstance(content, list)
        assert len(content) == 2
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"

    def test_multimodal_content_with_video_is_rejected(self):
        """OpenAI Chat Completions 不把视频伪装成 image_url。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.USER,
                [Text("看看这个视频"), Video.from_bytes(_MP4_BYTES)],
            )
        ]
        with pytest.raises(UnsupportedModalityError, match="视频"):
            _payloads_to_openai_messages(payloads)

    def test_multimodal_content_with_audio(self):
        """测试多模态内容（文本+已验证音频）。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.USER,
                [Text("转写这段音频"), Audio.from_bytes(_MP3_BYTES)],
            )
        ]
        messages, tools = _payloads_to_openai_messages(payloads)

        assert len(messages) == 1
        assert tools == []
        content = messages[0]["content"]
        assert isinstance(content, list)
        assert len(content) == 2
        assert content[1]["type"] == "input_audio"
        assert content[1]["input_audio"] == {
            "data": _b64(_MP3_BYTES),
            "format": "mp3",
        }

    def test_audio_format_propagates_wav(self):
        """已验证 audio/wav 应输出 format='wav'。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [LLMPayload(ROLE.USER, [Audio.from_bytes(_WAV_BYTES)])]
        messages, _ = _payloads_to_openai_messages(payloads)
        assert messages[0]["content"][0]["input_audio"] == {
            "data": _b64(_WAV_BYTES),
            "format": "wav",
        }

    def test_audio_format_alias_is_canonicalized(self):
        """audio/x-wav 在 IR 层规范化为 audio/wav。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        audio = Audio.from_bytes(_WAV_BYTES, mime_type="audio/x-wav")
        messages, _ = _payloads_to_openai_messages([LLMPayload(ROLE.USER, [audio])])
        assert messages[0]["content"][0]["input_audio"]["format"] == "wav"

    def test_audio_unknown_wire_mime_is_rejected(self):
        """Provider 不为未知音频 MIME 猜测 mp3。"""
        from src.kernel.llm.model_client.openai_client import _audio_mime_to_format

        with pytest.raises(UnsupportedModalityError, match="audio/silk"):
            _audio_mime_to_format("audio/silk")

    def test_audio_data_url_extracts_mime_type(self):
        """data URL 的真实 MIME 应体现在 OpenAI format 字段。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payload = f"data:audio/wav;base64,{_b64(_WAV_BYTES)}"
        messages, _ = _payloads_to_openai_messages(
            [LLMPayload(ROLE.USER, [Audio(payload)])]
        )
        assert messages[0]["content"][0]["input_audio"]["format"] == "wav"

    def test_tool_payload(self):
        """测试工具定义payload。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        class MockTool:
            @classmethod
            def to_schema(cls):
                return {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object"},
                }

        payloads = [LLMPayload(ROLE.TOOL, MockTool)]
        messages, tools = _payloads_to_openai_messages(payloads)

        # TOOL payload不应该进入messages
        assert len(messages) == 0
        assert len(tools) == 1
        assert tools[0]["type"] == "function"
        assert tools[0]["function"]["name"] == "test_tool"

    def test_tool_result_payload(self):
        """测试工具结果payload。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        result = ToolResult(value="42", call_id="call_123", name="calculator")
        payloads = [LLMPayload(ROLE.TOOL_RESULT, [result])]

        messages, tools = _payloads_to_openai_messages(payloads)

        assert len(messages) == 1
        assert messages[0]["role"] == "tool"
        assert messages[0]["content"] == "42"
        assert messages[0]["tool_call_id"] == "call_123"

    def test_tool_result_with_text(self):
        """测试工具结果包含Text。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.TOOL_RESULT,
                [ToolResult(value="result", call_id="call_123"), Text("extra")],
            )
        ]

        messages, tools = _payloads_to_openai_messages(payloads)

        assert len(messages) == 1
        # 应该使用ToolResult的内容
        assert messages[0]["content"] == "result"

    def test_multiple_tool_results_in_one_payload(self):
        """同一个 TOOL_RESULT payload 内包含多个 ToolResult 时应拆分为多条 tool message。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages

        payloads = [
            LLMPayload(
                ROLE.TOOL_RESULT,
                [
                    ToolResult(value="a", call_id="call_a"),
                    ToolResult(value="b", call_id="call_b"),
                ],
            )
        ]

        messages, tools = _payloads_to_openai_messages(payloads)

        assert tools == []
        assert len(messages) == 2
        assert messages[0]["role"] == "tool"
        assert messages[0]["tool_call_id"] == "call_a"
        assert messages[0]["content"] == "a"
        assert messages[1]["role"] == "tool"
        assert messages[1]["tool_call_id"] == "call_b"
        assert messages[1]["content"] == "b"

    def test_custom_object_with_to_text(self):
        """测试自定义对象带to_text方法。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages
        from src.kernel.llm.payload.content import Content

        class CustomResult(Content):
            call_id = "custom_call"

            def to_text(self):
                return "custom result"

        payloads = [LLMPayload(ROLE.TOOL_RESULT, [CustomResult()])]

        messages, tools = _payloads_to_openai_messages(payloads)

        assert len(messages) == 1
        assert messages[0]["content"] == "custom result"
        assert messages[0]["tool_call_id"] == "custom_call"

    def test_empty_tool_result_content(self):
        """测试空的工具结果内容。"""
        from src.kernel.llm.model_client.openai_client import _payloads_to_openai_messages
        from src.kernel.llm.payload.content import Content

        class BrokenObject(Content):
            def to_text(self):
                raise Exception("Cannot convert")

        payloads = [LLMPayload(ROLE.TOOL_RESULT, [BrokenObject()])]

        messages, tools = _payloads_to_openai_messages(payloads)

        assert len(messages) == 1
        assert messages[0]["content"] == ""


class TestSchemaNormalization:
    """测试 schema 归一化逻辑。"""

    def test_normalize_array_and_remove_none_default(self):
        """测试补齐 array.items 并移除 default=None。"""
        from src.kernel.llm.model_client.openai_client import _normalize_schema_for_grammar

        schema = {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "default": None,
                }
            },
        }

        _normalize_schema_for_grammar(schema)
        tags = schema["properties"]["tags"]
        assert "default" not in tags
        assert tags["items"]["type"] == "string"

    def test_to_openai_tool_normalizes_function_schema(self):
        """测试 _to_openai_tool 会归一化函数参数 schema。"""
        from src.kernel.llm.model_client.openai_client import _to_openai_tool

        class MockTool:
            @classmethod
            def to_schema(cls):
                return {
                    "name": "test_tool",
                    "description": "desc",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "tags": {
                                "type": "array",
                                "default": None,
                            }
                        },
                    },
                }

        tool = _to_openai_tool(MockTool)
        tags = tool["function"]["parameters"]["properties"]["tags"]
        assert tags["type"] == "array"
        assert tags["items"]["type"] == "string"
        assert "default" not in tags

    def test_to_openai_tool_keeps_existing_reason_schema(self):
        """测试已有 reason 参数时不重复注入。"""
        from src.kernel.llm.model_client.openai_client import _to_openai_tool

        class MockTool:
            @classmethod
            def to_schema(cls):
                return {
                    "name": "test_tool",
                    "description": "desc",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "description": "original reason",
                            },
                            "query": {
                                "type": "string",
                                "description": "query",
                            },
                        },
                        "required": ["query"],
                    },
                }

        tool = _to_openai_tool(MockTool)
        properties = tool["function"]["parameters"]["properties"]
        required = tool["function"]["parameters"]["required"]

        assert properties["reason"]["description"] == "original reason"
        assert required == ["query"]

    def test_to_openai_tool_does_not_inject_reason_when_execute_accepts_it(self):
        """测试 execute 已声明 reason 时不额外注入 schema 参数。"""
        from src.kernel.llm.model_client.openai_client import _to_openai_tool

        class MockTool:
            @classmethod
            def to_schema(cls):
                return {
                    "name": "test_tool",
                    "description": "desc",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "query",
                            }
                        },
                        "required": ["query"],
                    },
                }

            async def execute(self, query: str, reason: str) -> tuple[bool, str]:
                return True, f"{query}:{reason}"

        tool = _to_openai_tool(MockTool)
        properties = tool["function"]["parameters"]["properties"]
        required = tool["function"]["parameters"]["required"]

        assert "reason" not in properties
        assert "reason" not in required


class TestOpenAIChatClient:
    """测试OpenAIChatClient类。"""

    def test_init(self):
        """测试初始化。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        client = OpenAIChatClient()

        assert client._clients == {}
        assert hasattr(client, "_lock")

    def test_get_loop_key_with_running_loop(self):
        """测试获取事件循环key（有运行中的循环）。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        client = OpenAIChatClient()

        async def test():
            loop_key = client._get_loop_key()
            assert loop_key > 0
            return loop_key

        loop_key = asyncio.run(test())
        assert isinstance(loop_key, int)

    def test_get_loop_key_without_loop(self):
        """测试获取事件循环key（无运行中的循环）。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        client = OpenAIChatClient()

        # 在没有运行循环的情况下调用
        loop_key = client._get_loop_key()
        assert loop_key == 0

    async def test_create_non_streaming_response(self):
        """测试非流式响应。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        # 创建mock客户端
        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "Hello, world!"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}  # 清空缓存
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [LLMPayload(ROLE.USER, Text("Hi"))]
        model_set = {
            "api_key": "test-key",
            "base_url": "https://api.test.com",
            "timeout": 30.0,
            "max_tokens": 100,
            "temperature": 0.7,
            "extra_params": {},
        }

        message, tool_calls, stream_iter, reasoning_content, request_record_id = await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        assert message == "Hello, world!"
        assert tool_calls == []
        assert stream_iter is None
        assert reasoning_content is None
        assert isinstance(request_record_id, int)

    async def test_create_stores_usage_with_cache_fields(self):
        """测试非流式请求会记录 usage，并包含缓存相关字段。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "Hello"
        mock_completion.choices[0].message.tool_calls = None
        mock_completion.usage = SimpleNamespace(
            prompt_tokens=120,
            completion_tokens=30,
            total_tokens=150,
            prompt_tokens_details=SimpleNamespace(
                cache_read_input_tokens=80,
                cache_creation_input_tokens=20,
            ),
        )

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [LLMPayload(ROLE.USER, Text("Hi"))]
        model_set = {
            "api_key": "test-key",
            "base_url": "https://api.test.com",
            "timeout": 30.0,
            "max_tokens": 100,
            "temperature": 0.7,
            "extra_params": {},
        }

        await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        usage = client.pop_last_usage()
        assert usage is not None
        assert usage["prompt_tokens"] == 120
        assert usage["completion_tokens"] == 30
        assert usage["total_tokens"] == 150
        assert usage["cache_read_input_tokens"] == 80
        assert usage["cache_creation_input_tokens"] == 20
        assert client.pop_last_usage() is None

    async def test_create_with_tool_calls(self):
        """测试包含工具调用的响应。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        # 创建mock工具调用
        mock_tc1 = MagicMock()
        mock_tc1.id = "call_123"
        mock_tc1.function.name = "calculator"
        mock_tc1.function.arguments = '{"a": 1, "b": 2}'

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = None
        mock_completion.choices[0].message.tool_calls = [mock_tc1]

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [LLMPayload(ROLE.USER, Text("Calculate 1+2"))]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": {},
        }

        message, tool_calls, stream_iter, reasoning_content, request_record_id = await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        assert message == ""
        assert tool_calls is not None
        assert len(tool_calls) == 1
        assert tool_calls[0]["id"] == "call_123"
        assert tool_calls[0]["name"] == "calculator"
        assert tool_calls[0]["args"] == {"a": 1, "b": 2}
        assert reasoning_content is None
        assert isinstance(request_record_id, int)

    async def test_create_omits_tool_choice_without_tool_payloads(self):
        """测试未传工具 payload 时不会透传 tool_choice。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "Hello, world!"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [LLMPayload(ROLE.USER, Text("Hi"))]
        model_set = {
            "api_key": "test-key",
            "base_url": "https://api.test.com",
            "timeout": 30.0,
            "extra_params": {"tool_choice": "auto"},
        }

        await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        await_args = mock_chat.completions.create.await_args
        assert await_args is not None
        create_kwargs = await_args.kwargs
        assert "tools" not in create_kwargs
        assert "tool_choice" not in create_kwargs

    async def test_create_keeps_configured_tool_choice_with_tool_payloads(self):
        """测试传入工具 payload 时保留配置的 tool_choice。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        class MockTool:
            @classmethod
            def to_schema(cls):
                return {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object"},
                }

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "Hello, world!"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [
            LLMPayload(ROLE.USER, Text("Hi")),
            LLMPayload(ROLE.TOOL, MockTool),
        ]
        model_set = {
            "api_key": "test-key",
            "base_url": "https://api.test.com",
            "timeout": 30.0,
            "extra_params": {"tool_choice": "auto"},
        }

        await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        await_args = mock_chat.completions.create.await_args
        assert await_args is not None
        create_kwargs = await_args.kwargs
        assert create_kwargs["tool_choice"] == "auto"
        assert create_kwargs["tools"][0]["function"]["name"] == "test_tool"

    async def test_create_invalid_model_set_type(self):
        """测试无效的model_set类型。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        client = OpenAIChatClient()

        with pytest.raises(TypeError, match="OpenAIChatClient 期望 model_set 为单个模型配置 dict"):
            await client.create(
                model_name="gpt-4",
                payloads=[],
                tools=[],
                request_name="test",
                model_set="not_a_dict",  # type: ignore
                stream=False,
            )

    async def test_create_missing_api_key(self):
        """测试缺少api_key。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        client = OpenAIChatClient()
        model_set = {"api_key": ""}  # 空api_key

        with pytest.raises(ValueError, match="model.api_key 不能为空"):
            await client.create(
                model_name="gpt-4",
                payloads=[],
                tools=[],
                request_name="test",
                model_set=model_set,
                stream=False,
            )

    async def test_create_invalid_extra_params(self):
        """测试无效的extra_params。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "OK"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": "invalid",  # 不是dict
        }

        with pytest.raises(ValueError, match="model.extra_params 必须是 dict"):
            await client.create(
                model_name="gpt-4",
                payloads=[],
                tools=[],
                request_name="test",
                model_set=model_set,
                stream=False,
            )

    async def test_create_with_extra_params(self):
        """测试包含额外参数。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "Response"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [LLMPayload(ROLE.USER, Text("Hi"))]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": {"top_p": 0.9, "presence_penalty": 0.1},
        }

        await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        # 验证额外参数被传递
        call_kwargs = mock_chat.completions.create.call_args.kwargs
        assert call_kwargs["top_p"] == 0.9
        assert call_kwargs["presence_penalty"] == 0.1

    async def test_create_rejects_video_before_provider_call(self):
        """视频在 OpenAI provider 序列化前明确失败，不能伪装成图片或重试。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_chat = AsyncMock()
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat

        client = OpenAIChatClient()
        client._get_client = MagicMock(return_value=mock_openai_client)
        payloads = [
            LLMPayload(ROLE.USER, [Text("描述一下"), Video.from_bytes(_MP4_BYTES)])
        ]

        with pytest.raises(UnsupportedModalityError, match="视频"):
            await client.create(
                model_name="gpt-4o",
                payloads=payloads,
                tools=[],
                request_name="test",
                model_set={"api_key": "test-key", "extra_params": {}},
                stream=False,
            )

        mock_chat.assert_not_awaited()

    async def test_create_rejects_video_without_provider_retry(self):
        """Provider 侧不根据错误文本做视频删除/重试。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_chat = AsyncMock()
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat
        client = OpenAIChatClient()
        client._get_client = MagicMock(return_value=mock_openai_client)

        with pytest.raises(UnsupportedModalityError):
            await client.create(
                model_name="mimo-v2.5",
                payloads=[LLMPayload(ROLE.USER, [Video.from_bytes(_MP4_BYTES)])],
                tools=[],
                request_name="test",
                model_set={"api_key": "test-key", "extra_params": {}},
                stream=False,
            )
        mock_chat.assert_not_awaited()

    async def test_create_preserves_provider_image_error_without_text_fallback(self):
        """图片被端点拒绝时保留上游错误，不删图也不改成文字请求。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        upstream_error = Exception(
            "Error code: 404 - {'error': {'message': 'No endpoints found that support image input'}}"
        )
        mock_chat = AsyncMock(side_effect=upstream_error)
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat
        client = OpenAIChatClient()
        client._get_client = MagicMock(return_value=mock_openai_client)

        with pytest.raises(Exception, match="No endpoints found that support image input"):
            await client.create(
                model_name="mimo-v2.5-pro",
                payloads=[
                    LLMPayload(
                        ROLE.USER,
                        [Text("描述一下"), Image.from_bytes(_PNG_BYTES)],
                    )
                ],
                tools=[],
                request_name="test",
                model_set={"api_key": "test-key", "extra_params": {}},
                stream=False,
            )
        mock_chat.assert_awaited_once()

    async def test_life_inspect_media_native_image_error_is_preserved_for_chatter_fallback(self):
        """tool-inspect_media 提升媒体时，不在底层静默删图，让 life_chatter 生成文字观察回退。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        upstream_error = Exception(
            "Error code: 404 - {'error': {'message': 'No endpoints found that support image input'}}"
        )

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(side_effect=upstream_error)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [
            LLMPayload(
                ROLE.USER,
                [
                    Text("你刚刚调用了 tool-inspect_media。以下媒体已被提升为原生多模态输入。"),
                    Image.from_bytes(_PNG_BYTES),
                ],
            )
        ]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": {},
        }

        with pytest.raises(Exception, match="No endpoints found that support image input"):
            await client.create(
                model_name="mimo-v2.5-pro",
                payloads=payloads,
                tools=[],
                request_name="life_chatter",
                model_set=model_set,
                stream=False,
            )

        assert mock_chat.completions.create.await_count == 1

    async def test_create_stream_preserves_provider_image_error(self):
        """流式图片请求失败时同样不删除图片块或自动重试。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        upstream_error = Exception(
            "Error code: 404 - {'error': {'message': 'No endpoints found that support image input'}}"
        )
        mock_chat = AsyncMock(side_effect=upstream_error)
        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat
        client = OpenAIChatClient()
        client._get_client = MagicMock(return_value=mock_openai_client)

        with pytest.raises(Exception, match="No endpoints found that support image input"):
            await client.create(
                model_name="mimo-v2.5-pro",
                payloads=[
                    LLMPayload(ROLE.USER, [Text("描述一下"), Image.from_bytes(_PNG_BYTES)])
                ],
                tools=[],
                request_name="test",
                model_set={"api_key": "test-key", "extra_params": {}},
                stream=True,
            )
        mock_chat.assert_awaited_once()

    async def test_create_inject_reasoning_content_for_thinking_tool_calls(self):
        """测试开启 thinking 时为 tool_calls 消息补齐 reasoning_content。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "OK"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [
            LLMPayload(ROLE.USER, Text("Hi")),
            LLMPayload(
                ROLE.ASSISTANT,
                [
                    Text("I'll call a tool"),
                    ToolCall(id="call_1", name="calculator", args={"a": 1}),
                ],
            ),
        ]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": {"enable_thinking": True},
        }

        await client.create(
            model_name="moonshotai/kimi-k2.5",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        call_kwargs = mock_chat.completions.create.call_args.kwargs
        messages = call_kwargs["messages"]
        tool_call_message = messages[1]
        assert tool_call_message["role"] == "assistant"
        assert "tool_calls" in tool_call_message
        assert "reasoning_content" in tool_call_message
        assert tool_call_message["reasoning_content"] == tool_call_message["content"]

    async def test_create_inject_reasoning_content_for_thinking_param(self):
        """测试 Anthropic 风格 thinking 参数也会启用 reasoning_content 回填。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "OK"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [
            LLMPayload(ROLE.USER, Text("Hi")),
            LLMPayload(
                ROLE.ASSISTANT,
                [
                    Text("I'll call a tool"),
                    ToolCall(id="call_1", name="calculator", args={"a": 1}),
                ],
            ),
        ]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": {"thinking": {"type": "enabled", "budget_tokens": 1000}},
        }

        await client.create(
            model_name="glm-5.2",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        messages = mock_chat.completions.create.call_args.kwargs["messages"]
        tool_call_message = messages[1]
        assert "reasoning_content" in tool_call_message
        assert tool_call_message["reasoning_content"] == tool_call_message["content"]

    async def test_create_backfills_reasoning_content_when_history_already_contains_it(self):
        """测试当上下文中已有 reasoning_content 时，会为其他 assistant 历史回填。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "OK"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [
            LLMPayload(ROLE.USER, Text("Hi")),
            LLMPayload(ROLE.ASSISTANT, [ReasoningText("先思考"), Text("已有思考回复")]),
            LLMPayload(
                ROLE.ASSISTANT,
                [
                    Text("I'll call a tool"),
                    ToolCall(id="call_1", name="calculator", args={"a": 1}),
                ],
            ),
        ]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": {},
        }

        await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        call_kwargs = mock_chat.completions.create.call_args.kwargs
        messages = call_kwargs["messages"]
        first_assistant_message = messages[1]
        tool_call_message = messages[2]
        assert first_assistant_message["reasoning_content"] == "先思考"
        assert tool_call_message["role"] == "assistant"
        assert tool_call_message["reasoning_content"] == tool_call_message["content"]

    async def test_create_does_not_backfill_reasoning_content_without_reasoning_history(self):
        """测试当历史中没有 reasoning_content 时，不会额外回填。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "OK"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [
            LLMPayload(ROLE.USER, Text("Hi")),
            LLMPayload(
                ROLE.ASSISTANT,
                [
                    Text("I'll call a tool"),
                    ToolCall(id="call_1", name="calculator", args={"a": 1}),
                ],
            ),
        ]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": {},
        }

        await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        call_kwargs = mock_chat.completions.create.call_args.kwargs
        messages = call_kwargs["messages"]
        tool_call_message = messages[1]
        assert tool_call_message["role"] == "assistant"
        assert "reasoning_content" not in tool_call_message

    async def test_create_preserves_reasoning_content_from_completion_message(self):
        """测试非流式响应中的 reasoning_content 会被返回。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "最终回答"
        mock_completion.choices[0].message.reasoning_content = "中间思考"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        result = await client.create(
            model_name="moonshotai/kimi-k2.5",
            payloads=[LLMPayload(ROLE.USER, Text("Hi"))],
            tools=[],
            request_name="test",
            model_set={
                "api_key": "test-key",
                "base_url": None,
                "timeout": None,
                "max_tokens": None,
                "temperature": None,
                "extra_params": {"enable_thinking": True},
            },
            stream=False,
        )

        assert result[0] == "最终回答"
        assert result[3] == "中间思考"

    async def test_default_tool_choice_is_auto_for_deepseek(self):
        """测试 DeepSeek 场景下默认 tool_choice 为 auto（避免 required 触发 grammar 编译失败）。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        class MockTool:
            @classmethod
            def to_schema(cls):
                return {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {
                        "type": "object",
                        "properties": {"x": {"type": "string"}},
                        "required": ["x"],
                    },
                }

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "Response"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [
            LLMPayload(ROLE.USER, Text("Hi")),
            LLMPayload(ROLE.TOOL, MockTool),
        ]
        model_set = {
            "api_key": "test-key",
            "base_url": "https://api.deepseek.com",
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": {},
        }

        await client.create(
            model_name="deepseek-ai/deepseek-v3.1",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        call_kwargs = mock_chat.completions.create.call_args.kwargs
        assert call_kwargs["tool_choice"] == "auto"

    async def test_default_tool_choice_is_required_for_non_deepseek(self):
        """测试非 DeepSeek 提供商默认 tool_choice 也为 auto。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        class MockTool:
            @classmethod
            def to_schema(cls):
                return {
                    "name": "test_tool",
                    "description": "A test tool",
                    "parameters": {"type": "object"},
                }

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "Response"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [
            LLMPayload(ROLE.USER, Text("Hi")),
            LLMPayload(ROLE.TOOL, MockTool),
        ]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "extra_params": {},
        }

        await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        call_kwargs = mock_chat.completions.create.call_args.kwargs
        assert call_kwargs["tool_choice"] == "auto"

    async def test_create_with_tool_call_compat_prompt_and_parsing(self):
        """测试 tool_call_compat 模式注入提示词并解析 JSON 返回。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        class MockTool:
            @classmethod
            def to_schema(cls):
                return {
                    "name": "calculator",
                    "description": "calc",
                    "parameters": {"type": "object", "properties": {"a": {"type": "number"}}},
                }

        mock_completion = MagicMock()
        mock_completion.choices = [MagicMock()]
        mock_completion.choices[0].message.content = "{'tool_calls':[{'name':'calculator','args':{'a':1}}]}"
        mock_completion.choices[0].message.tool_calls = None

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=mock_completion)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [
            LLMPayload(ROLE.USER, Text("请计算")),
            LLMPayload(ROLE.TOOL, MockTool),
        ]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": None,
            "max_tokens": None,
            "temperature": None,
            "tool_call_compat": True,
            "extra_params": {},
        }

        message, tool_calls, _, reasoning_content, request_record_id = await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=False,
        )

        call_kwargs = mock_chat.completions.create.call_args.kwargs
        assert "tools" not in call_kwargs
        assert isinstance(call_kwargs["messages"][-1]["content"], str)
        assert "请只返回一个 JSON 对象" in call_kwargs["messages"][-1]["content"]
        assert "可用工具 schema" in call_kwargs["messages"][-1]["content"]

        assert message == ""
        assert tool_calls is not None
        assert len(tool_calls) == 1
        assert tool_calls[0]["name"] == "calculator"
        assert tool_calls[0]["args"] == {"a": 1}
        assert reasoning_content is None
        assert isinstance(request_record_id, int)

    async def test_stream_iterator_closes_underlying_stream_on_early_stop(self):
        """测试流式响应提前停止消费时会关闭底层流对象。"""
        from src.kernel.llm.model_client.openai_client import OpenAIChatClient

        class FakeStream:
            def __init__(self):
                self._index = 0
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self._index > 0:
                    raise StopAsyncIteration
                self._index += 1

                delta = MagicMock()
                delta.content = "hello"
                delta.tool_calls = None
                delta.function_call = None

                choice = MagicMock()
                choice.delta = delta

                chunk = MagicMock()
                chunk.choices = [choice]
                return chunk

            async def aclose(self):
                self.closed = True

        fake_stream = FakeStream()

        mock_chat = AsyncMock()
        mock_chat.completions.create = AsyncMock(return_value=fake_stream)

        mock_openai_client = MagicMock()
        mock_openai_client.chat.completions.create = mock_chat.completions.create

        client = OpenAIChatClient()
        client._clients = {}
        client._get_client = MagicMock(return_value=mock_openai_client)

        payloads = [LLMPayload(ROLE.USER, Text("Hi"))]
        model_set = {
            "api_key": "test-key",
            "base_url": None,
            "timeout": 30.0,
            "max_tokens": 100,
            "temperature": 0.7,
            "extra_params": {},
        }

        message, tool_calls, stream_iter, reasoning_content, request_record_id = await client.create(
            model_name="gpt-4",
            payloads=payloads,
            tools=[],
            request_name="test",
            model_set=model_set,
            stream=True,
        )

        assert message is None
        assert tool_calls is None
        assert stream_iter is not None
        assert reasoning_content is None
        assert isinstance(request_record_id, int)

        event = await anext(stream_iter)
        assert event.text_delta == "hello"

        await cast(Any, stream_iter).aclose()
        assert fake_stream.closed is True
