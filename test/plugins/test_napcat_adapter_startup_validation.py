"""测试 napcat_adapter 启动时的身份配置校验。"""

from __future__ import annotations

import asyncio
import hmac
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, Mock, patch

import pytest

from plugins.napcat_adapter.config import NapcatAdapterConfig
from plugins.napcat_adapter.events.meta import MetaEventHandler
from plugins.napcat_adapter.plugin import (
    NapcatAdapter,
    NapcatAdapterPlugin,
    _validate_bot_identity,
)
from src.core.transport.wire import WebSocketAdapterOptions


def test_disabled_napcat_plugin_registers_no_adapter_component() -> None:
    """plugin.enabled=false 必须阻止 Adapter 注册和连接启动。"""
    config = NapcatAdapterConfig.from_dict(
        {
            "plugin": {"enabled": False, "config_version": "2.0.0"},
            "bot": {"qq_id": "123456789", "qq_nickname": "Elysia"},
            "napcat_server": {
                "mode": "reverse",
                "host": "localhost",
                "port": 8095,
                "access_token": "",
            },
        }
    )

    assert NapcatAdapterPlugin(config=config).get_components() == []


def test_new_napcat_install_defaults_disabled_and_missing_config_fails_closed() -> None:
    assert NapcatAdapterConfig.PluginSection().enabled is False
    assert NapcatAdapterPlugin(config=None).get_components() == []


def test_enabled_napcat_plugin_still_registers_adapter_component() -> None:
    """修复停用开关不能破坏 enabled=true 的正常注册。"""
    plugin = _build_napcat_plugin()

    assert plugin.get_components() == [NapcatAdapter]


def _build_napcat_plugin(*, access_token: str = "") -> NapcatAdapterPlugin:
    """构造测试用 Napcat 插件实例。"""
    config = NapcatAdapterConfig.from_dict(
        {
            "plugin": {"enabled": True, "config_version": "2.0.0"},
            "bot": {"qq_id": "123456789", "qq_nickname": "Elysia"},
            "napcat_server": {
                "mode": "reverse",
                "host": "localhost",
                "port": 8095,
                "access_token": access_token,
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


async def _capture_reverse_ws_handler(
    adapter: NapcatAdapter,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, dict[str, Any]]:
    """Capture the legacy-server handler without opening a real socket."""
    captured: dict[str, Any] = {}

    async def fake_serve(handler: Any, host: str, port: int, **kwargs: Any) -> Any:
        captured.update(handler=handler, host=host, port=port, kwargs=kwargs)
        return SimpleNamespace(is_serving=lambda: True)

    monkeypatch.setattr("websockets.legacy.server.serve", fake_serve)
    options = cast(WebSocketAdapterOptions, adapter._transport_config)
    await adapter._start_ws_server(options)
    return captured["handler"], captured


def _fake_reverse_ws(authorization: str | None = None) -> Any:
    headers = {} if authorization is None else {"Authorization": authorization}
    return SimpleNamespace(path="/", request_headers=headers, close=AsyncMock())


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
                "bot": {"qq_id": "123456789", "qq_nickname": "Elysia"},
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
                "bot": {"qq_id": "", "qq_nickname": "Elysia"},
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
                "bot": {"qq_id": "abc123", "qq_nickname": "Elysia"},
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
            "bot": {"qq_id": "123456789", "qq_nickname": "Elysia"},
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
        "bot_name": "Elysia",
        "platform": "qq",
    }


def test_send_platform_message_propagates_sender_error() -> None:
    """NapCat v3 出站发送失败时不应被适配器吞掉异常。"""
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(),
    )
    adapter._sender.send = AsyncMock(side_effect=ValueError("bad target"))

    envelope = {
        "message_info": {},
        "message_segment": {"type": "text", "data": "hello"},
    }
    with pytest.raises(ValueError, match="bad target"):
        asyncio.run(adapter._send_platform_message(envelope))


def test_watchdog_uses_managed_task_contract_for_shutdown() -> None:
    """Watchdog cleanup must cancel TaskInfo through TaskManager."""

    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(),
    )
    task_info = SimpleNamespace(
        task_id="napcat-watchdog",
        is_done=Mock(return_value=False),
    )
    manager = SimpleNamespace(
        create_task=Mock(),
        cancel_task=Mock(return_value=True),
    )
    adapter._watchdog_task = cast(Any, task_info)

    with patch(
        "plugins.napcat_adapter.plugin.get_task_manager",
        return_value=manager,
    ):
        adapter._start_watchdog()
        adapter._stop_watchdog()

    manager.create_task.assert_not_called()
    manager.cancel_task.assert_called_once_with("napcat-watchdog")
    assert adapter._watchdog_task is None


async def test_send_platform_message_preserves_napcat_timeout_cause() -> None:
    """Napcat API 超时应保留原始 TimeoutError 异常链。"""
    plugin = _build_napcat_plugin()
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)
    adapter._client.call = AsyncMock(side_effect=TimeoutError("napcat timeout"))
    envelope = {
        "message_info": {
            "user_info": {"user_id": "987654"},
        },
        "message_segment": [
            {"type": "text", "data": "timeout"},
        ],
    }

    with pytest.raises(TimeoutError, match="napcat timeout"):
        await adapter._send_platform_message(envelope)

    adapter._client.call.assert_awaited_once()


def test_send_normal_message_splits_multiline_text_into_multiple_napcat_messages() -> None:
    """NapCat 出站文本包含换行时，应拆成多条消息发送。"""
    plugin = _build_napcat_plugin()
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)
    adapter._client.call = AsyncMock(return_value={"status": "ok"})

    envelope = {
        "message_info": {
            "group_info": {"group_id": "123456"},
            "user_info": {"user_id": "987654"},
        },
        "message_segment": [
            {"type": "text", "data": "第一行\n第二行"},
        ],
    }

    asyncio.run(adapter._sender.send(envelope))

    assert adapter._client.call.await_count == 2
    first_call = adapter._client.call.await_args_list[0]
    second_call = adapter._client.call.await_args_list[1]

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
    adapter._client.call = AsyncMock(return_value={"status": "ok"})

    envelope = {
        "message_info": {
            "user_info": {"user_id": "987654"},
        },
        "message_segment": [
            {"type": "text", "data": "只有一行"},
        ],
    }

    asyncio.run(adapter._sender.send(envelope))

    assert adapter._client.call.await_count == 1
    only_call = adapter._client.call.await_args_list[0]
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
            "bot": {"qq_id": "123456789", "qq_nickname": "Elysia"},
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
        adapter.from_platform_message(
            {
                "post_type": "message",
                "self_id": "123456789",
                "message_id": 1001,
                "message_type": "private",
                "sender": {
                    "user_id": "123456789",
                    "nickname": "Elysia",
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
            "bot": {"qq_id": "123456789", "qq_nickname": "Elysia"},
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
        adapter.from_platform_message(
            {
                "post_type": "message",
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


async def test_meta_event_handler_does_not_reconnect_on_advisory_status() -> None:
    """online/good 是 QQ 会话建议状态，不是 WebSocket 断线证据。"""
    plugin = _build_napcat_plugin()
    adapter = NapcatAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin)
    adapter.reconnect = AsyncMock()
    adapter._router.meta_handler.set_reconnect_callback(adapter.reconnect)
    adapter._router.meta_handler._checking = True

    await adapter._router.meta_handler.handle(
        {
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "self_id": 123456789,
            "status": {"online": False, "good": False},
            "interval": 30000,
        }
    )

    adapter.reconnect.assert_not_awaited()
    assert adapter._router.meta_handler._reported_status_degraded is True


async def test_get_close_wait_sockets_keeps_only_matching_pid_and_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    """CLOSE-WAIT 解析必须保留精确 fd，不能退化为 NapCat 的全部 socket。"""
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(),
    )
    process = AsyncMock()
    process.communicate.return_value = (
        b"State Recv-Q Send-Q Local Address:Port Peer Address:Port Process\n"
        b'CLOSE-WAIT 0 0 172.26.1.2:49832 198.18.0.29:443 users:(("qq",pid=1901,fd=175))\n'
        b'CLOSE-WAIT 0 0 172.26.1.2:49833 198.18.0.30:443 users:(("qq",pid=19010,fd=176))\n',
        b"",
    )
    create_process = AsyncMock(return_value=process)
    monkeypatch.setattr(
        "plugins.napcat_adapter.plugin.asyncio.create_subprocess_exec",
        create_process,
    )

    result = await adapter._get_close_wait_sockets(1901)

    assert result == [("172.26.1.2:49832 -> 198.18.0.29:443", 175)]


async def test_napcat_socket_cleanup_does_not_expand_to_all_fds() -> None:
    """WSL2 清理只允许操作 ss 指定的 fd。"""
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(),
    )

    result = await adapter._get_napcat_socket_fds(
        1901,
        [("172.26.1.2:49832 -> 198.18.0.29:443", 175)],
    )

    assert result == [175]


def test_napcat_health_does_not_use_business_message_inactivity() -> None:
    """安静连接是正常状态，不能按多久没业务消息触发重连。"""
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(),
    )

    assert not hasattr(adapter, "_activity_timeout")
    assert not hasattr(adapter, "_last_message_time")
    assert not hasattr(adapter, "_watchdog_thread")


async def test_advisory_offline_heartbeat_refreshes_transport_liveness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = MetaEventHandler(cast(Any, object()), lambda: None)
    handler._checking = True
    monkeypatch.setattr(
        "plugins.napcat_adapter.events.meta.time.monotonic",
        lambda: 123.0,
    )

    await handler.handle(
        {
            "meta_event_type": "heartbeat",
            "self_id": 3427056465,
            "interval": 30000,
            "status": {"online": False, "good": True},
        }
    )

    assert handler._last_heartbeat == 123.0
    assert handler._interval == 30.0


async def test_back_to_back_heartbeats_create_one_checker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = MetaEventHandler(cast(Any, object()), lambda: None)
    scheduled: list[Any] = []

    class _TaskManager:
        def create_task(self, coroutine: Any, **_kwargs: Any) -> Mock:
            scheduled.append(coroutine)
            return Mock()

    monkeypatch.setattr(
        "plugins.napcat_adapter.events.meta.get_task_manager",
        lambda: _TaskManager(),
    )

    event = {
        "meta_event_type": "heartbeat",
        "self_id": 3427056465,
        "interval": 30000,
        "status": {"online": True, "good": True},
    }
    await handler.handle(event)
    await handler.handle(event)

    assert handler._checking is True
    assert len(scheduled) == 1
    scheduled[0].close()


async def test_healthy_heartbeat_clears_advisory_degraded_state() -> None:
    handler = MetaEventHandler(cast(Any, object()), lambda: None)
    handler._checking = True
    handler._reported_status_degraded = True

    await handler.handle(
        {
            "meta_event_type": "heartbeat",
            "self_id": 3427056465,
            "status": {"online": True, "good": True},
        }
    )

    assert handler._reported_status_degraded is False


def test_heartbeat_timeout_allows_three_reporting_periods() -> None:
    handler = MetaEventHandler(cast(Any, object()), lambda: None)
    handler._interval = 30.0

    assert handler._heartbeat_timeout_seconds() == 90.0


async def test_reverse_mode_health_owns_listener_not_client_connection() -> None:
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(),
    )
    adapter._ws = None
    adapter._ws_server = SimpleNamespace(is_serving=lambda: True)

    assert await adapter.health_check() is True


async def test_reverse_mode_health_fails_when_listener_is_missing() -> None:
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(),
    )
    adapter._ws = None
    adapter._ws_server = None

    assert await adapter.health_check() is False


async def test_reverse_ws_accepts_matching_bearer_without_response_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-reverse-token"
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(access_token=secret),
    )
    connected = AsyncMock()
    disconnected = AsyncMock()
    listen = AsyncMock()
    monkeypatch.setattr(adapter, "on_ws_connected", connected)
    monkeypatch.setattr(adapter, "on_ws_disconnected", disconnected)
    monkeypatch.setattr(adapter, "_ws_listen_loop", listen)

    handler, captured = await _capture_reverse_ws_handler(adapter, monkeypatch)
    ws = _fake_reverse_ws(f"Bearer {secret}")
    with patch(
        "plugins.napcat_adapter.plugin.hmac.compare_digest",
        wraps=hmac.compare_digest,
    ) as compare_digest:
        await handler(ws)

    options = cast(WebSocketAdapterOptions, adapter._transport_config)
    assert options.headers is None
    assert "extra_headers" not in captured["kwargs"]
    compare_digest.assert_called_once()
    ws.close.assert_not_awaited()
    connected.assert_awaited_once_with(ws)
    listen.assert_awaited_once_with(options)
    disconnected.assert_awaited_once_with()


async def test_reverse_ws_rejects_wrong_bearer_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "test-reverse-token"
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(access_token=secret),
    )
    connected = AsyncMock()
    listen = AsyncMock()
    warning = Mock()
    monkeypatch.setattr(adapter, "on_ws_connected", connected)
    monkeypatch.setattr(adapter, "_ws_listen_loop", listen)
    monkeypatch.setattr("plugins.napcat_adapter.plugin.logger.warning", warning)

    handler, _captured = await _capture_reverse_ws_handler(adapter, monkeypatch)
    ws = _fake_reverse_ws("Bearer wrong-token")
    await handler(ws)

    ws.close.assert_awaited_once_with(code=4401, reason="Unauthorized")
    connected.assert_not_awaited()
    listen.assert_not_awaited()
    assert adapter._ws is None
    assert secret not in repr(warning.call_args_list)
    assert secret not in repr(ws.close.await_args_list)


async def test_reverse_ws_rejects_missing_bearer_without_taking_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(access_token="test-reverse-token"),
    )
    connected = AsyncMock()
    listen = AsyncMock()
    monkeypatch.setattr(adapter, "on_ws_connected", connected)
    monkeypatch.setattr(adapter, "_ws_listen_loop", listen)

    handler, _captured = await _capture_reverse_ws_handler(adapter, monkeypatch)
    ws = _fake_reverse_ws()
    await handler(ws)

    ws.close.assert_awaited_once_with(code=4401, reason="Unauthorized")
    connected.assert_not_awaited()
    listen.assert_not_awaited()
    assert adapter._ws is None


async def test_reverse_ws_empty_token_preserves_unauthenticated_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=_build_napcat_plugin(access_token=""),
    )
    connected = AsyncMock()
    disconnected = AsyncMock()
    listen = AsyncMock()
    monkeypatch.setattr(adapter, "on_ws_connected", connected)
    monkeypatch.setattr(adapter, "on_ws_disconnected", disconnected)
    monkeypatch.setattr(adapter, "_ws_listen_loop", listen)

    handler, _captured = await _capture_reverse_ws_handler(adapter, monkeypatch)
    ws = _fake_reverse_ws()
    with patch("plugins.napcat_adapter.plugin.hmac.compare_digest") as compare_digest:
        await handler(ws)

    compare_digest.assert_not_called()
    ws.close.assert_not_awaited()
    connected.assert_awaited_once_with(ws)
    listen.assert_awaited_once()
    disconnected.assert_awaited_once_with()


async def test_qq_explicit_identity_mapping_reaches_envelope() -> None:
    plugin = _build_napcat_plugin()
    plugin.config.identity.account_identity_aliases = [
        "1419893769=wander_hunter"
    ]
    adapter = NapcatAdapter(
        core_sink=cast(Any, _FakeCoreSink()),
        plugin=plugin,
    )

    envelope = await adapter.from_platform_message(
        {
            "post_type": "message",
            "self_id": "3427056465",
            "message_id": 1003,
            "message_type": "private",
            "sender": {
                "user_id": "1419893769",
                "nickname": "Wandering Hunter",
            },
            "message": [{"type": "text", "data": {"text": "hello"}}],
        }
    )

    assert envelope is not None
    extra = envelope["message_info"]["extra"]
    assert extra["sender_platform_account_key"] == "qq:1419893769"
    assert extra["canonical_person_key"] == "wander_hunter"
    assert extra["identity_resolution_status"] == "resolved"
