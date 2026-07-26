"""skill_manager 命令测试。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from plugins.skill_manager.commands import SkillManagerCommand
from plugins.skill_manager.config import SkillManagerConfig
from plugins.skill_manager.models import SkillEntry
from plugins.skill_manager.plugin import SkillManagerPlugin
from src.core.prompt import get_system_reminder_store, reset_system_reminder_store


def _build_plugin() -> SkillManagerPlugin:
    """创建测试用插件实例。"""

    return SkillManagerPlugin(config=SkillManagerConfig())


def _register_skill(plugin: SkillManagerPlugin, root_dir: Path, name: str) -> SkillEntry:
    """注册一个临时 skill 供命令测试。"""

    skill_md_path = root_dir / "SKILL.md"
    skill_md_path.parent.mkdir(parents=True, exist_ok=True)
    skill_md_path.write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n",
        encoding="utf-8",
    )
    entry = SkillEntry(
        name=name,
        description=f"{name} skill",
        root_dir=root_dir,
        skill_md_path=skill_md_path,
        files=["SKILL.md"],
    )
    typed_plugin = cast(Any, plugin)
    typed_plugin.skills[name] = entry
    return entry


async def test_skill_command_list_reports_registered_skills(tmp_path: Path) -> None:
    """list 子命令应按名称列出已注册 skill。"""

    plugin = _build_plugin()
    _register_skill(plugin, tmp_path / "beta", "beta")
    _register_skill(plugin, tmp_path / "alpha", "alpha")
    command = SkillManagerCommand(plugin=plugin, stream_id="stream")

    with patch(
        "plugins.skill_manager.commands.skill_command.send_text",
        new=AsyncMock(),
    ) as send_text_mock:
        success, result = await command.execute("list")

    assert success is True
    assert cast(str, result).startswith("当前已索引 2 个 skill")
    await_args = send_text_mock.await_args
    assert await_args is not None
    sent_message = cast(str, await_args.args[0])
    assert sent_message.index("alpha") < sent_message.index("beta")
    assert "alpha: alpha skill" in sent_message
    assert "beta: beta skill" in sent_message


async def test_skill_command_refresh_reloads_catalog(tmp_path: Path) -> None:
    """refresh 子命令应触发插件重新扫描 skill 目录。"""

    plugin = _build_plugin()
    config = cast(SkillManagerConfig, plugin.config)
    config.manager.paths = [str(tmp_path)]
    skill_root = tmp_path / "demo"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: demo\ndescription: demo skill\n---\n",
        encoding="utf-8",
    )
    command = SkillManagerCommand(plugin=plugin, stream_id="stream")

    with patch(
        "plugins.skill_manager.commands.skill_command.send_text",
        new=AsyncMock(),
    ) as send_text_mock:
        success, result = await command.execute("refresh")

    assert success is True
    assert result == "已刷新 skill 索引，共 1 个。"
    typed_plugin = cast(Any, plugin)
    assert "demo" in typed_plugin.skills
    send_text_mock.assert_awaited_once_with(
        "已刷新 skill 索引，共 1 个。",
        stream_id="stream",
    )


def test_skill_manager_plugin_exposes_command_component() -> None:
    """插件组件列表应包含 SkillManagerCommand。"""

    plugin = _build_plugin()

    assert SkillManagerCommand in plugin.get_components()


async def test_skill_command_direct_invocation_injects_skill(
    tmp_path: Path,
) -> None:
    """应兼容旧的 /<skill-name> 直接注入体验。"""

    reset_system_reminder_store()
    plugin = _build_plugin()
    plugin.get_components()
    entry = _register_skill(plugin, tmp_path / "demo", "demo")
    entry.skill_md_path.write_text(
        "---\nname: demo\ndescription: demo skill\n---\n\nHello $ARGUMENTS",
        encoding="utf-8",
    )
    message = SimpleNamespace(content="/demo Neo", stream_id="stream")
    command = SkillManagerCommand(
        plugin=plugin,
        stream_id="stream",
        message=cast(Any, message),
    )

    with patch(
        "plugins.skill_manager.commands.skill_command.send_text",
        new=AsyncMock(),
    ) as send_text_mock:
        success, result = await command.execute("Neo")

    assert success is True
    assert result == "已注入 skill: demo"
    assert "demo" in cast(Any, plugin).injected_skills
    reminder = get_system_reminder_store().get("actor", names=["skill_manager_direct_demo"])
    assert "Hello Neo" in reminder
    send_text_mock.assert_awaited_once_with("已注入 skill: demo", stream_id="stream")
