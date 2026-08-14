"""commands_plugin 清空上下文命令测试。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from plugins.commands_plugin.commands import ClearContextCommand, PermCommand
from plugins.commands_plugin.config import CommandsPluginConfig
from plugins.commands_plugin.dispatch import CommandDispatchEventHandler
from plugins.commands_plugin.plugin import CommandsPlugin
from src.core.models.stream import ChatStream


def _build_plugin() -> CommandsPlugin:
    """创建测试用插件实例。"""
    return CommandsPlugin(
        CommandsPluginConfig.model_validate({"plugin": {"enabled": True}})
    )


def test_commands_plugin_defaults_to_no_components() -> None:
    """缺配置与默认配置均不得注册危险命令或消息分流。"""

    assert CommandsPlugin().get_components() == []
    default_config = CommandsPluginConfig.model_validate(CommandsPluginConfig.default())
    assert default_config.plugin.enabled is False
    assert CommandsPlugin(default_config).get_components() == []


def test_commands_plugin_explicit_enable_preserves_all_components() -> None:
    """显式启用后应完整注册现有命令能力。"""

    assert _build_plugin().get_components() == [
        CommandDispatchEventHandler,
        ClearContextCommand,
        PermCommand,
    ]


def test_utility_commands_plugin_exposes_clear_command() -> None:
    """插件应暴露清空上下文命令组件。"""
    plugin = _build_plugin()

    assert ClearContextCommand in plugin.get_components()


async def test_clear_context_command_clears_current_stream() -> None:
    """根命令应清空当前流上下文。"""
    plugin = _build_plugin()
    command = ClearContextCommand(plugin=plugin, stream_id="stream-current")

    with (
        patch(
            "plugins.commands_plugin.commands.clear_command.send_text",
            new=AsyncMock(),
        ) as send_text_mock,
        patch(
            "plugins.commands_plugin.commands.clear_command.stream_api.load_and_clear_context",
            new=AsyncMock(return_value=None),
        ) as clear_mock,
    ):
        success, result = await command.execute("")

    assert success is True
    assert result == "cleared current"
    clear_mock.assert_awaited_once_with("stream-current")
    send_text_mock.assert_awaited_once_with(
        "✓ 当前聊天上下文已清空。", stream_id="stream-current"
    )


async def test_clear_context_command_clears_specific_group_stream() -> None:
    """指定群号时应根据当前平台生成目标 stream_id。"""
    plugin = _build_plugin()
    command = ClearContextCommand(plugin=plugin, stream_id="stream-current")
    target_stream_id = ChatStream.generate_stream_id("qq", group_id="12345")

    with (
        patch(
            "plugins.commands_plugin.commands.clear_command.send_text",
            new=AsyncMock(),
        ) as send_text_mock,
        patch(
            "plugins.commands_plugin.commands.clear_command.stream_api.get_stream_info",
            new=AsyncMock(return_value={"platform": "qq"}),
        ) as info_mock,
        patch(
            "plugins.commands_plugin.commands.clear_command.stream_api.load_and_clear_context",
            new=AsyncMock(return_value=None),
        ) as clear_mock,
    ):
        success, result = await command.execute("群 12345")

    assert success is True
    assert result == "cleared group"
    info_mock.assert_awaited_once_with("stream-current")
    clear_mock.assert_awaited_once_with(target_stream_id)
    send_text_mock.assert_awaited_once_with(
        "✓ 群 12345 的上下文已清空。", stream_id="stream-current"
    )


async def test_clear_context_command_bulk_clear_all() -> None:
    """全部子命令应调用批量清空接口。"""
    plugin = _build_plugin()
    command = ClearContextCommand(plugin=plugin, stream_id="stream-current")

    with (
        patch(
            "plugins.commands_plugin.commands.clear_command.send_text",
            new=AsyncMock(),
        ) as send_text_mock,
        patch(
            "plugins.commands_plugin.commands.clear_command.stream_api.bulk_clear_streams",
            new=AsyncMock(return_value=7),
        ) as bulk_mock,
    ):
        success, result = await command.execute("全部")

    assert success is True
    assert result == "cleared 7 streams"
    bulk_mock.assert_awaited_once_with()
    send_text_mock.assert_awaited_once_with(
        "✓ 已清空 7 个聊天流的上下文。", stream_id="stream-current"
    )
