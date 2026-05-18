"""utility_commands 清空上下文命令。"""

from __future__ import annotations

from src.app.plugin_system.api import stream_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel
from src.core.models.stream import ChatStream

logger = get_logger("utility_commands.clear_command")


class ClearContextCommand(BaseCommand):
    """清空聊天流上下文命令。"""

    command_name: str = "清空上下文"
    command_description: str = "清空聊天流上下文（仅主人可用）"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """匹配命令名，同时支持中文和英文别名。"""
        if not parts:
            return 0
        if parts[0] in ("清空上下文", "clearctx"):
            return 1
        return 0

    async def _reply(self, text: str) -> None:
        """向当前聊天流回复文本。"""
        await send_text(text, stream_id=self.stream_id)

    async def _get_current_platform(self) -> str:
        """获取当前流所在平台。"""
        info = await stream_api.get_stream_info(self.stream_id)
        platform = info.get("platform") if info else None
        return str(platform or "")

    @cmd_route()
    async def handle_clear_current(self) -> tuple[bool, str]:
        """清空当前聊天流的上下文。"""
        await stream_api.load_and_clear_context(self.stream_id)
        await self._reply("✓ 当前聊天上下文已清空。")
        logger.info(f"已清空当前流上下文: {self.stream_id}")
        return True, "cleared current"

    @cmd_route("群")
    async def handle_clear_group(self, group_id: str = "") -> tuple[bool, str]:
        """清空群聊上下文。"""
        if not group_id:
            count = await stream_api.bulk_clear_streams("group")
            await self._reply(f"✓ 已清空 {count} 个群聊的上下文。")
            logger.info(f"已批量清空群聊上下文: {count}")
            return True, f"cleared {count} group streams"

        platform = await self._get_current_platform()
        if not platform:
            await self._reply("无法获取当前平台信息，请在有效会话中执行。")
            return False, "missing platform"

        target_stream_id = ChatStream.generate_stream_id(platform, group_id=group_id)
        await stream_api.load_and_clear_context(target_stream_id)
        await self._reply(f"✓ 群 {group_id} 的上下文已清空。")
        logger.info(f"已清空群上下文: stream_id={target_stream_id}")
        return True, "cleared group"

    @cmd_route("group")
    async def handle_clear_group_en(self, group_id: str = "") -> tuple[bool, str]:
        """清空群聊上下文（英文别名）。"""
        return await self.handle_clear_group(group_id)

    @cmd_route("私")
    async def handle_clear_private(self, user_id: str = "") -> tuple[bool, str]:
        """清空私聊上下文。"""
        if not user_id:
            count = await stream_api.bulk_clear_streams("private")
            await self._reply(f"✓ 已清空 {count} 个私聊的上下文。")
            logger.info(f"已批量清空私聊上下文: {count}")
            return True, f"cleared {count} private streams"

        platform = await self._get_current_platform()
        if not platform:
            await self._reply("无法获取当前平台信息，请在有效会话中执行。")
            return False, "missing platform"

        target_stream_id = ChatStream.generate_stream_id(platform, user_id=user_id)
        await stream_api.load_and_clear_context(target_stream_id)
        await self._reply(f"✓ 私聊 {user_id} 的上下文已清空。")
        logger.info(f"已清空私聊上下文: stream_id={target_stream_id}")
        return True, "cleared private"

    @cmd_route("private")
    async def handle_clear_private_en(self, user_id: str = "") -> tuple[bool, str]:
        """清空私聊上下文（英文别名）。"""
        return await self.handle_clear_private(user_id)

    @cmd_route("all")
    async def handle_clear_all(self) -> tuple[bool, str]:
        """清空所有聊天流上下文。"""
        count = await stream_api.bulk_clear_streams()
        await self._reply(f"✓ 已清空 {count} 个聊天流的上下文。")
        logger.info(f"已批量清空所有流上下文: {count}")
        return True, f"cleared {count} streams"

    @cmd_route("全部")
    async def handle_clear_all_cn(self) -> tuple[bool, str]:
        """清空所有聊天流上下文（中文别名）。"""
        return await self.handle_clear_all()
