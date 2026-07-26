"""core 层测试 fixtures。"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, Mock

import pytest


@pytest.fixture
def mock_plugin():
    """创建模拟插件的 fixture。"""
    plugin = MagicMock()
    plugin.plugin_name = "test_plugin"
    plugin.plugin_description = "Test plugin"
    plugin.plugin_version = "1.0.0"
    plugin.get_components = Mock(return_value=[])
    plugin.on_plugin_loaded = AsyncMock()
    plugin.on_plugin_unloaded = AsyncMock()
    return plugin


@pytest.fixture
def mock_chat_stream():
    """创建模拟聊天流的 fixture。"""
    stream = MagicMock()
    stream.stream_id = "test_stream_123"
    stream.chat_type = "group"
    stream.platform = "test_platform"

    context = MagicMock()
    context.history_messages = []

    mock_message = MagicMock()
    mock_message.processed_plain_text = "Hello world"
    mock_message.content = "Hello world"
    mock_message.sender_name = "TestUser"
    context.history_messages.append(mock_message)

    stream.context = context
    return stream
