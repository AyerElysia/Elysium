"""Ayla 适配器契约测试。

覆盖文档 `docs/architecture/Elysium接入Ayla平台模块.md` §3/§8.1：
- AylaAdapter.platform == "ayla"，注册形态正确；
- plugin.enabled=false 不注册 Adapter 组件（与 napcat 对齐）；
- on_adapter_loaded 只做配置校验，不建立连接；
- health_check 返回配置有效性（不是 is_connected，避免误重连）；
- from_platform_message 不接收入站（入站走 inject）；
- _send_platform_message 出站虚拟确认不抛错、不向 Ayla 应用重复投递；
- get_bot_info 返回 bot 身份（bot_id=elysia / bot_name=爱莉 / platform=ayla）；
- 出站 MessageEnvelope 的文本内容审计不记录完整正文。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from plugins.ayla_adapter.config import AylaAdapterConfig
from plugins.ayla_adapter.plugin import AylaAdapter, AylaAdapterPlugin
from plugins.ayla_adapter.sender import AylaSender


class _FakeCoreSink:
    """满足 BaseAdapter 初始化所需的最小 CoreSink 替身。"""

    def set_outgoing_handler(self, _handler) -> None: ...

    def remove_outgoing_handler(self, _handler) -> None: ...

    async def push_outgoing(self, _message) -> None: ...

    async def close(self) -> None: ...

    async def send(self, _message) -> None: ...

    async def send_many(self, _messages) -> None: ...


def _config(**overrides: Any) -> AylaAdapterConfig:
    plugin = {"enabled": True, "config_version": "1.0.0"}
    backend = {"backend_url": "", "bot_name": "爱莉"}
    data = {"plugin": plugin, "backend": backend}
    data["plugin"].update(overrides.get("plugin", {}))
    data["backend"].update(overrides.get("backend", {}))
    return AylaAdapterConfig.from_dict(data)


def _plugin(**overrides: Any) -> AylaAdapterPlugin:
    return AylaAdapterPlugin(config=_config(**overrides))


def _adapter(plugin: AylaAdapterPlugin | None = None) -> AylaAdapter:
    return AylaAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=plugin or _plugin())


def test_platform_is_ayla() -> None:
    assert AylaAdapter.platform == "ayla"
    assert AylaAdapter.adapter_name == "ayla_adapter"


def test_disabled_plugin_registers_no_adapter_component() -> None:
    plugin = _plugin(plugin={"enabled": False})
    assert plugin.get_components() == []


def test_enabled_plugin_registers_adapter_component() -> None:
    plugin = _plugin()
    assert plugin.get_components() == [AylaAdapter]


async def test_loaded_validates_config_without_connecting() -> None:
    """on_adapter_loaded 只做配置校验，不建立任何连接。"""
    adapter = _adapter()
    await adapter.on_adapter_loaded()
    assert adapter.sender is not None
    assert isinstance(adapter.sender, AylaSender)


async def test_loaded_without_config_raises() -> None:
    adapter = AylaAdapter(core_sink=cast(Any, _FakeCoreSink()), plugin=None)
    with pytest.raises(RuntimeError, match="缺少插件配置"):
        await adapter.on_adapter_loaded()


async def test_health_check_returns_config_validity_not_connection() -> None:
    """Ayla 无长连接，health_check 必须返回配置有效性，避免误判重连。"""
    adapter = _adapter()
    await adapter.on_adapter_loaded()
    assert await adapter.health_check() is True

    unloaded = _adapter()
    assert await unloaded.health_check() is False


async def test_from_platform_message_returns_none() -> None:
    """本 Adapter 不接收入站（入站走 inject）。"""
    adapter = _adapter()
    envelope = await adapter.from_platform_message({"post_type": "message"})
    assert envelope is None


async def test_send_platform_message_acknowledges_without_repeat_delivery() -> None:
    """出站虚拟确认不抛错；不向 Ayla 应用重复投递（SSE 投影是唯一通道）。"""
    adapter = _adapter()
    await adapter.on_adapter_loaded()
    assert adapter.sender is not None
    adapter.sender.send = AsyncMock()  # 观测调用；不实际投递

    envelope = {
        "message_info": {
            "platform": "ayla",
            "user_info": {"user_id": "user-42"},
        },
        "message_segment": [{"type": "text", "data": "你好，汐汐"}],
    }
    await adapter._send_platform_message(envelope)

    adapter.sender.send.assert_awaited_once()
    sent: dict[str, Any] = adapter.sender.send.await_args.args[0]
    assert sent["message_info"]["platform"] == "ayla"


async def test_get_bot_info_returns_standard_identity() -> None:
    adapter = _adapter(_plugin(backend={"bot_name": "爱莉"}))
    bot_info = await adapter.get_bot_info()
    assert bot_info == {
        "bot_id": "elysia",
        "bot_name": "爱莉",
        "platform": "ayla",
    }


def test_sender_preview_truncates_and_omits_private_content() -> None:
    """审计摘要截断且不含私有身份/完整正文。"""
    long_text = "长" * 120
    envelope = {
        "message_info": {"user_info": {"user_id": "secret-uid"}},
        "message_segment": [{"type": "text", "data": long_text}],
    }
    preview = AylaSender._extract_text_preview(envelope)
    assert len(preview) == 51  # 50 字符 + "…"
    assert "secret-uid" not in preview
    assert "长" * 51 not in preview
