"""SkillManager 插件配置。"""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base import BaseConfig
from src.kernel.config.core import Field, SectionBase, config_section


class SkillManagerConfig(BaseConfig):
    """SkillManager 配置模型。"""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "SkillManager 配置"

    @config_section("plugin", title="插件设置", tag="plugin")
    class PluginSection(SectionBase):
        """Top-level activation boundary for all SkillManager components."""

        enabled: bool = Field(
            default=False,
            description="是否注册 SkillManager 索引、命令与工具",
        )

    @config_section("manager", title="技能管理", tag="plugin")
    class ManagerSection(SectionBase):
        """技能管理主配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用 SkillManager",
        )
        paths: list[str] = Field(
            default_factory=lambda: [
                "data/life_engine_workspace/skills",
            ],
            description="skill 根目录路径列表；相对路径默认相对项目根目录。",
        )
        inject_actor_reminder: bool = Field(
            default=True,
            description="是否注入 actor system reminder",
        )
        inject_sub_actor_reminder: bool = Field(
            default=True,
            description="是否注入 sub_actor system reminder",
        )
        max_catalog_chars: int = Field(
            default=4096,
            ge=512,
            description="注入 system reminder 的 skill 清单最大字符数",
        )
        max_skill_body_chars: int = Field(
            default=8192,
            ge=512,
            description="get_skill 返回 SKILL.md 主文档的最大字符数",
        )
        max_reference_chars: int = Field(
            default=16384,
            ge=512,
            description="get_reference 返回引用文档的最大字符数",
        )
        allow_script_execution: bool = Field(
            default=False,
            description="是否允许 get_script 执行 skill 目录内脚本。关闭后仍可读取 skill 和引用文档。",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    manager: ManagerSection = Field(default_factory=ManagerSection)
