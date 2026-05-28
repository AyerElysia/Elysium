"""skill_manager 命令组件。"""

from __future__ import annotations

from typing import Any, Protocol, cast

from plugins.skill_manager.models import SkillEntry

from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel
from src.core.prompt import SystemReminderInsertType, get_system_reminder_store

from ..shared import limit_text, manager_config_value, read_cached_skill_content


class _SkillManagerPluginProtocol(Protocol):
    """SkillManagerCommand 依赖的最小插件接口。"""

    skills: dict[str, SkillEntry]
    skill_contents: dict[str, str]
    injected_skills: set[str]
    config: Any

    async def refresh_skill_catalog(self) -> None:
        """刷新 skill 索引。"""


_USAGE = """/skill 用法：
  /skill list | 列表      - 列出当前已索引的 skill
  /skill refresh | 刷新   - 重新扫描并刷新 skill 索引
  /skill help | 帮助      - 显示本帮助

也支持直接调用：/<skill-name> [参数]，会把对应 SKILL.md 注入当前运行态。"""


class SkillManagerCommand(BaseCommand):
    """SkillManager 管理命令。"""

    command_name: str = "skill"
    command_description: str = "管理 SkillManager 索引：列出、刷新技能"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """匹配命令名，同时支持 skill 和 技能。"""

        if not parts:
            return 0
        if parts[0] in ("skill", "技能"):
            return 1
        try:
            plugin = getattr(cls, "_plugin_instance", None)
            if plugin is not None and parts[0] in getattr(plugin, "skills", {}):
                return 1
        except Exception:
            return 0
        return 0

    async def execute(self, message_text: str) -> tuple[bool, str]:
        """执行 /skill 子命令，兼容 /<skill-name> 直接注入。"""

        direct_name = self._get_direct_skill_name()
        if direct_name:
            return await self._handle_direct_skill_invocation(direct_name, message_text.strip())
        return await super().execute(message_text)

    async def _reply(self, text: str) -> None:
        """向当前聊天流发送文本回复。"""

        await send_text(text, stream_id=self.stream_id)

    def _get_plugin(self) -> _SkillManagerPluginProtocol:
        """返回带具体类型的插件实例。"""

        return cast(_SkillManagerPluginProtocol, self.plugin)

    def _get_direct_skill_name(self) -> str:
        """从原始消息中解析 /<skill-name> 直接调用。"""

        raw_text = str(getattr(self._message, "content", "") or "").strip()
        if not raw_text:
            return ""
        if raw_text.startswith(self.command_prefix):
            raw_text = raw_text[len(self.command_prefix):].strip()
        first = raw_text.split(maxsplit=1)[0] if raw_text else ""
        if first in ("skill", "技能"):
            return ""
        plugin = self._get_plugin()
        return first if first in plugin.skills else ""

    def _limit_skill_body(self, text: str) -> str:
        max_chars = manager_config_value(
            self._get_plugin(),
            "max_skill_body_chars",
            8192,
        )
        return limit_text(text, max_chars, "SKILL.md")

    async def _handle_direct_skill_invocation(self, name: str, args: str) -> tuple[bool, str]:
        """处理 /<skill-name> 直接注入。"""

        plugin = self._get_plugin()
        entry = plugin.skills.get(name)
        if entry is None:
            await self._reply(f"skill '{name}' 不存在。")
            return False, "not found"

        content = read_cached_skill_content(plugin, name, entry.skill_md_path)
        if args:
            content = content.replace("$ARGUMENTS", args)
        content = self._limit_skill_body(content)

        reminder_name = f"skill_manager_direct_{name}"
        store = get_system_reminder_store()
        store.set(
            "actor",
            name=reminder_name,
            content=content,
            insert_type=SystemReminderInsertType.DYNAMIC,
        )
        store.set(
            "sub_actor",
            name=reminder_name,
            content=content,
            insert_type=SystemReminderInsertType.DYNAMIC,
        )
        plugin.injected_skills.add(name)

        message = f"已注入 skill: {name}"
        await self._reply(message)
        return True, message

    def _render_skill_list(self) -> str:
        """渲染当前 skill 列表文本。"""

        plugin = self._get_plugin()
        if not plugin.skills:
            return "当前没有已索引的 skill，可先执行 /skill refresh 刷新。"

        entries = sorted(plugin.skills.values(), key=lambda item: item.name.lower())
        lines = [f"当前已索引 {len(entries)} 个 skill："]
        for entry in entries:
            lines.append(
                f"- {entry.name}: {entry.description} "
                f"(文件 {len(entry.files)}，路径 {entry.root_dir.name})"
            )
        return "\n".join(lines)

    @cmd_route()
    async def handle_default(self) -> tuple[bool, str]:
        """显示帮助和当前 skill 数量。"""

        plugin = self._get_plugin()
        summary = f"当前已索引 {len(plugin.skills)} 个 skill。"
        await self._reply(f"{summary}\n\n{_USAGE}")
        return True, summary

    @cmd_route("help")
    async def handle_help(self) -> tuple[bool, str]:
        """显示帮助信息。"""

        await self._reply(_USAGE)
        return True, "help"

    @cmd_route("帮助")
    async def handle_help_cn(self) -> tuple[bool, str]:
        """显示帮助信息（中文别名）。"""

        return await self.handle_help()

    @cmd_route("list")
    async def handle_list(self) -> tuple[bool, str]:
        """列出当前已索引的 skill。"""

        rendered = self._render_skill_list()
        await self._reply(rendered)
        return True, rendered

    @cmd_route("列表")
    async def handle_list_cn(self) -> tuple[bool, str]:
        """列出当前已索引的 skill（中文别名）。"""

        return await self.handle_list()

    @cmd_route("refresh")
    async def handle_refresh(self) -> tuple[bool, str]:
        """重新扫描配置路径并刷新 skill 索引。"""

        plugin = self._get_plugin()
        await plugin.refresh_skill_catalog()
        message = f"已刷新 skill 索引，共 {len(plugin.skills)} 个。"
        await self._reply(message)
        return True, message

    @cmd_route("刷新")
    async def handle_refresh_cn(self) -> tuple[bool, str]:
        """重新扫描配置路径并刷新 skill 索引（中文别名）。"""

        return await self.handle_refresh()
