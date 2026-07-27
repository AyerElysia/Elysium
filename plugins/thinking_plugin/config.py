"""Thinking Plugin 配置。

配置思考动作的提示词引导内容和启用状态，允许用户自定义思考习惯的引导方式。
"""

from typing import ClassVar

from src.core.components.base.config import BaseConfig, SectionBase, config_section, Field


class ThinkingConfig(BaseConfig):
    """Thinking Plugin 配置类。

    Attributes:
        config_name: 配置文件名称
        config_description: 配置描述
    """

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "思考动作插件配置"

    @config_section("settings")
    class SettingsSection(SectionBase):
        """基础设置配置项。"""

        enabled: bool = Field(
            default=True,
            description="是否启用思考动作。设为 false 可临时禁用思考功能，无需从 core.toml 移除插件。",
        )
        enable_trigger_reminder: bool = Field(
            default=True,
            description="是否启用思考触发器提醒。设为 false 可禁用每次回复前的醒目提醒，但仍保留 think action。",
        )

    @config_section("fields")
    class FieldsSection(SectionBase):
        """思考字段开关配置项。控制 think action 的字段要求。"""

        enable_mood: bool = Field(
            default=False,
            description="是否启用 mood（心情）字段。启用后，思考时需填写此刻的心情状态。",
        )
        enable_decision: bool = Field(
            default=False,
            description="是否启用 decision（决定）字段。启用后，思考时需填写下一步的决定。",
        )
        enable_expected_response: bool = Field(
            default=False,
            description="是否启用 expected_response（预期反应）字段。启用后，思考时需填写对用户反应的预期。",
        )

    @config_section("prompt")
    class PromptSection(SectionBase):
        """提示词配置项。"""

        thinking_habit: str = Field(
            default="",
            description="思考习惯引导提示词",
        )

    settings: SettingsSection = Field(default_factory=SettingsSection)
    fields: FieldsSection = Field(default_factory=FieldsSection)
    prompt: PromptSection = Field(default_factory=PromptSection)
