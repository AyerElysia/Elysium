"""测试 napcat_adapter 启动时的身份配置校验。"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from plugins.napcat_adapter.config import NapcatAdapterConfig
from plugins.napcat_adapter.plugin import NapcatAdapter, NapcatAdapterPlugin, _validate_bot_identity


def _build_napcat_plugin() -> NapcatAdapterPlugin:
    """构造测试用 Napcat 插件实例。"""
    config = NapcatAdapterConfig.from_dict(
        {
            "plugin": {"enabled": True, "config_version": "2.0.0"},
            "bot": {"qq_id": "123456789", "qq_nickname": "MoFoxBot"},
            "napcat_server": {
                "mode": "reverse",
                "host": "localhost",
                "port": 8095,
                "access_token": "",
            },
            "features": {
                "group_list_type": "blacklist",
                "group_list": [],
                "private_list_type": "blacklist",
                "private_list": [],
                "ban_user_id": [],
                "enable_poke": True,
                "ignore_non_self_poke": False,
                "poke_debounce_seconds": 2.0,
                "enable_emoji_like": True,
                "enable_reply_at": True,
                "reply_at_rate": 0.5,
                "enable_video_processing": True,
                "video_max_size_mb": 100,
                "video_download_timeout": 60,
            },
        }
    )
    return NapcatAdapterPlugin(config=config)


class _FakeCoreSink:
    """满足 BaseAdapter 初始化所需的最小 CoreSink 替身。"""

    def set_outgoing_handler(self, _handler) -> None:
        """设置发送处理器。"""

    def remove_outgoing_handler(self, _handler) -> None:
        """移除发送处理器。"""

    async def push_outgoing(self, _message) -> None:
        """推送单条外发消息。"""

    async def close(self) -> None:
        """关闭 sink。"""

    async def send(self, _message) -> None:
        """发送单条消息。"""

    async def send_many(self, _messages) -> None:
        """发送多条消息。"""


class TestNapcatAdapterStartupValidation:
    """测试 Napcat 适配器启动校验。"""

    def test_validate_bot_identity_accepts_valid_values(self) -> None:
        """有效配置应通过校验。"""
        config = NapcatAdapterConfig.from_dict(
            {
                "plugin": {"enabled": True, "config_version": "2.0.0"},
                "bot": {"qq_id": "123456789", "qq_nickname": "MoFoxBot"},
                "napcat_server": {
                    "mode": "reverse",
                    "host": "localhost",
                    "port": 8095,
                    "access_token": "",
                },
                "features": {
                    "group_list_type": "blacklist",
                    "group_list": [],
                    "private_list_type": "blacklist",
                    "private_list": [],
                    "ban_user_id": [],
                    "enable_poke": True,
                    "ignore_non_self_poke": False,
                    "poke_debounce_seconds": 2.0,
                    "enable_emoji_like": True,
                    "enable_reply_at": True,
                    "reply_at_rate": 0.5,
                    "enable_video_processing": True,
                    "video_max_size_mb": 100,
                    "video_download_timeout": 60,
                },
            }
        )

        _validate_bot_identity(config)

    def test_validate_bot_identity_rejects_empty_qq_id(self) -> None:
        """空 qq_id 应被拒绝。"""
        config = NapcatAdapterConfig.from_dict(
            {
                "plugin": {"enabled": True, "config_version": "2.0.0"},
                "bot": {"qq_id": "", "qq_nickname": "MoFoxBot"},
                "napcat_server": {
                    "mode": "reverse",
                    "host": "localhost",
                    "port": 8095,
                    "access_token": "",
                },
                "features": {
                    "group_list_type": "blacklist",
                    "group_list": [],
                    "private_list_type": "blacklist",
                    "private_list": [],
                    "ban_user_id": [],
                    "enable_poke": True,
                    "ignore_non_self_poke": False,
                    "poke_debounce_seconds": 2.0,
                    "enable_emoji_like": True,
                    "enable_reply_at": True,
                    "reply_at_rate": 0.5,
                    "enable_video_processing": True,
                    "video_max_size_mb": 100,
                    "video_download_timeout": 60,
                },
            }
        )

        with pytest.raises(ValueError, match="bot.qq_id"):
            _validate_bot_identity(config)

    def test_validate_bot_identity_rejects_non_digit_qq_id(self) -> None:
        """非数字 qq_id 应被拒绝。"""
        config = NapcatAdapterConfig.from_dict(
            {
                "plugin": {"enabled": True, "config_version": "2.0.0"},
                "bot": {"qq_id": "abc123", "qq_nickname": "MoFoxBot"},
                "napcat_server": {
                    "mode": "reverse",
                    "host": "localhost",
                    "port": 8095,
                    "access_token": "",
                },
                "features": {
                    "group_list_type": "blacklist",
                    "group_list": [],
                    "private_list_type": "blacklist",
                    "private_list": [],
                    "ban_user_id": [],
                    "enable_poke": True,
                    "ignore_non_self_poke": False,
                    "poke_debounce_seconds": 2.0,
                    "enable_emoji_like": True,
                    "enable_reply_at": True,
                    "reply_at_rate": 0.5,
                    "enable_video_processing": True,
                    "video_max_size_mb": 100,
                    "video_download_timeout": 60,
                },
            }
        )

        with pytest.raises(ValueError, match="bot.qq_id"):
            _validate_bot_identity(config)

    def test_validate_bot_identity_rejects_empty_nickname(self) -> None:
        """空 qq_nickname 应被拒绝。"""
        config = NapcatAdapterConfig.from_dict(
            {
                "plugin": {"enabled": True, "config_version": "2.0.0"},
                "bot": {"qq_id": "123456789", "qq_nickname": "   "},
                "napcat_server": {
                    "mode": "reverse",
                    "host": "localhost",
                    "port": 8095,
                    "access_token": "",
                },
                "features": {
                    "group_list_type": "blacklist",
                    "group_list": [],
                    "private_list_type": "blacklist",
                    "private_list": [],
                    "ban_user_id": [],
                    "enable_poke": True,
                    "ignore_non_self_poke": False,
                    "poke_debounce_seconds": 2.0,
                    "enable_emoji_like": True,
                    "enable_reply_at": True,
                    "reply_at_rate": 0.5,
                    "enable_video_processing": True,
                    "video_max_size_mb": 100,
                    "video_download_timeout": 60,
                },
            }
        )

        with pytest.raises(ValueError, match="bot.qq_nickname"):
            _validate_bot_identity(config)


def test_get_bot_info_returns_standard_bot_name_field() -> None:
    """NapcatAdapter 应按统一契约返回 bot_name。"""
    config = NapcatAdapterConfig.from_dict(
        {
            "plugin": {"enabled": True, "config_version": "2.0.0"},
            "bot": {"qq_id": "123456789", "qq_nickname": "MoFoxBot"},
            "napcat_server": {
                "mode": "reverse",
                "host": "localhost",
                "port": 8095,
                "access_token": "",
            },
            "features": {
                "group_list_type": "blacklist",
                "group_list": [],
                "private_list_type": "blacklist",
                "private_list": [],
                "ban_user_id": [],
                "enable_poke": True,
                "ignore_non_self_poke": False,
                "poke_debounce_seconds": 2.0,
                "enable_emoji_like": True,
                "enable_reply_at": True,
                "reply_at_rate": 0.5,
                "enable_video_processing": True,
                "video_max_size_mb": 100,
                "video_download_timeout": 60,
            },
        }
    )
    plugin = NapcatAdapterPlugin(config=config)
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)

    bot_info = asyncio.run(adapter.get_bot_info())

    assert bot_info == {
        "bot_id": "123456789",
        "bot_name": "MoFoxBot",
        "platform": "qq",
    }


def test_send_platform_message_propagates_send_handler_error() -> None:
    """Napcat 发送失败时不应被适配器吞掉异常。"""
    config = NapcatAdapterConfig.from_dict(
        {
            "plugin": {"enabled": True, "config_version": "2.0.0"},
            "bot": {"qq_id": "123456789", "qq_nickname": "MoFoxBot"},
            "napcat_server": {
                "mode": "reverse",
                "host": "localhost",
                "port": 8095,
                "access_token": "",
            },
            "features": {
                "group_list_type": "blacklist",
                "group_list": [],
                "private_list_type": "blacklist",
                "private_list": [],
                "ban_user_id": [],
                "enable_poke": True,
                "ignore_non_self_poke": False,
                "poke_debounce_seconds": 2.0,
                "enable_emoji_like": True,
                "enable_reply_at": True,
                "reply_at_rate": 0.5,
                "enable_video_processing": True,
                "video_max_size_mb": 100,
                "video_download_timeout": 60,
            },
        }
    )
    plugin = NapcatAdapterPlugin(config=config)
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)
    adapter.send_handler.handle_message = AsyncMock(side_effect=ValueError("bad target"))

    with pytest.raises(ValueError, match="bad target"):
        asyncio.run(adapter._send_platform_message({"message_info": {}, "message_segment": {}}))


def test_send_normal_message_splits_multiline_text_into_multiple_napcat_messages() -> None:
    """NapCat 出站文本包含换行时，应拆成多条消息发送。"""
    plugin = _build_napcat_plugin()
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)
    adapter.send_handler.send_message_to_napcat = AsyncMock(
        return_value={"status": "ok"}
    )

    envelope = {
        "message_info": {
            "group_info": {"group_id": "123456"},
            "user_info": {"user_id": "987654"},
        },
        "message_segment": [
            {"type": "text", "data": "第一行\n第二行"},
        ],
    }

    asyncio.run(adapter.send_handler.send_normal_message(envelope))

    assert adapter.send_handler.send_message_to_napcat.await_count == 2
    first_call = adapter.send_handler.send_message_to_napcat.await_args_list[0]
    second_call = adapter.send_handler.send_message_to_napcat.await_args_list[1]

    assert first_call.args[0] == "send_group_msg"
    assert first_call.args[1]["group_id"] == 123456
    assert first_call.args[1]["message"] == [
        {"type": "text", "data": {"text": "第一行"}}
    ]

    assert second_call.args[0] == "send_group_msg"
    assert second_call.args[1]["group_id"] == 123456
    assert second_call.args[1]["message"] == [
        {"type": "text", "data": {"text": "第二行"}}
    ]


def test_send_normal_message_keeps_single_line_text_as_one_napcat_message() -> None:
    """普通单行文本应保持单条发送。"""
    plugin = _build_napcat_plugin()
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)
    adapter.send_handler.send_message_to_napcat = AsyncMock(
        return_value={"status": "ok"}
    )

    envelope = {
        "message_info": {
            "user_info": {"user_id": "987654"},
        },
        "message_segment": [
            {"type": "text", "data": "只有一行"},
        ],
    }

    asyncio.run(adapter.send_handler.send_normal_message(envelope))

    assert adapter.send_handler.send_message_to_napcat.await_count == 1
    only_call = adapter.send_handler.send_message_to_napcat.await_args_list[0]
    assert only_call.args[0] == "send_private_msg"
    assert only_call.args[1]["user_id"] == 987654
    assert only_call.args[1]["message"] == [
        {"type": "text", "data": {"text": "只有一行"}}
    ]


def test_handle_raw_message_ignores_bot_self_echo() -> None:
    """Napcat 回推 bot 自己的消息时，不应再次进入接收链路。"""
    config = NapcatAdapterConfig.from_dict(
        {
            "plugin": {"enabled": True, "config_version": "2.0.0"},
            "bot": {"qq_id": "123456789", "qq_nickname": "MoFoxBot"},
            "napcat_server": {
                "mode": "reverse",
                "host": "localhost",
                "port": 8095,
                "access_token": "",
            },
            "features": {
                "group_list_type": "blacklist",
                "group_list": [],
                "private_list_type": "blacklist",
                "private_list": [],
                "ban_user_id": [],
                "enable_poke": True,
                "ignore_non_self_poke": False,
                "poke_debounce_seconds": 2.0,
                "enable_emoji_like": True,
                "enable_reply_at": True,
                "reply_at_rate": 0.5,
                "enable_video_processing": True,
                "video_max_size_mb": 100,
                "video_download_timeout": 60,
            },
        }
    )
    plugin = NapcatAdapterPlugin(config=config)
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)

    envelope = asyncio.run(
        adapter.message_handler.handle_raw_message(
            {
                "self_id": "123456789",
                "message_id": 1001,
                "message_type": "private",
                "sender": {
                    "user_id": "123456789",
                    "nickname": "MoFoxBot",
                },
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )
    )

    assert envelope is None


def test_handle_raw_message_keeps_normal_private_message() -> None:
    """普通私聊消息不应被误判为 self echo。"""
    config = NapcatAdapterConfig.from_dict(
        {
            "plugin": {"enabled": True, "config_version": "2.0.0"},
            "bot": {"qq_id": "123456789", "qq_nickname": "MoFoxBot"},
            "napcat_server": {
                "mode": "reverse",
                "host": "localhost",
                "port": 8095,
                "access_token": "",
            },
            "features": {
                "group_list_type": "blacklist",
                "group_list": [],
                "private_list_type": "blacklist",
                "private_list": [],
                "ban_user_id": [],
                "enable_poke": True,
                "ignore_non_self_poke": False,
                "poke_debounce_seconds": 2.0,
                "enable_emoji_like": True,
                "enable_reply_at": True,
                "reply_at_rate": 0.5,
                "enable_video_processing": True,
                "video_max_size_mb": 100,
                "video_download_timeout": 60,
            },
        }
    )
    plugin = NapcatAdapterPlugin(config=config)
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)

    envelope = asyncio.run(
        adapter.message_handler.handle_raw_message(
            {
                "self_id": "123456789",
                "message_id": 1002,
                "message_type": "private",
                "sender": {
                    "user_id": "987654321",
                    "nickname": "Alice",
                },
                "message": [{"type": "text", "data": {"text": "hello"}}],
            }
        )
    )

    assert envelope is not None


@pytest.mark.asyncio
async def test_meta_event_handler_reconnects_when_heartbeat_times_out(monkeypatch) -> None:
    """心跳超时时应触发适配器自动重连。"""

    plugin = _build_napcat_plugin()
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)
    adapter.reconnect = AsyncMock()

    handler = adapter.meta_event_handler
    handler.last_heart_beat = 0.0
    handler.interval = 0.01

    monkeypatch.setattr(
        "plugins.napcat_adapter.src.handlers.to_core.meta_event_handler.time.time",
        lambda: 0.03,
    )

    await handler.check_heartbeat(123456789)

    adapter.reconnect.assert_awaited_once()
    assert handler._interval_checking is False
