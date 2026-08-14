"""Werewolf game plugin configuration."""

from __future__ import annotations

from typing import ClassVar

from src.core.components.base.config import BaseConfig, Field, SectionBase, config_section


class WerewolfConfig(BaseConfig):
    """Werewolf game plugin configuration."""

    config_name: ClassVar[str] = "config"
    config_description: ClassVar[str] = "狼人杀游戏配置"

    @config_section("plugin", title="插件设置", tag="plugin", order=0)
    class PluginSection(SectionBase):
        enabled: bool = Field(
            default=False,
            description="是否启用狼人杀插件",
            label="启用插件",
            tag="plugin",
            order=0,
        )

    @config_section("game", title="游戏规则", tag="game", order=1)
    class GameSection(SectionBase):
        default_board: str = Field(
            default="12人标准屠边局",
            description="默认板子名称",
            label="默认板子",
            tag="game",
            order=0,
        )
        night_timeout: int = Field(
            default=60,
            description="夜晚行动超时（秒）",
            label="夜晚超时",
            tag="game",
            order=1,
        )
        day_speech_timeout: int = Field(
            default=180,
            description="白天讨论超时（秒）",
            label="讨论超时",
            tag="game",
            order=2,
        )
        vote_timeout: int = Field(
            default=60,
            description="投票超时（秒）",
            label="投票超时",
            tag="game",
            order=3,
        )
        last_words_timeout: int = Field(
            default=30,
            description="遗言超时（秒）",
            label="遗言超时",
            tag="game",
            order=4,
        )

    @config_section("ai", title="AI 玩家", tag="ai", order=2)
    class AISection(SectionBase):
        enabled: bool = Field(
            default=True,
            description="是否允许 AI 玩家加入",
            label="AI 玩家",
            tag="ai",
            order=0,
        )
        difficulty: str = Field(
            default="normal",
            description="AI 难度（easy/normal/hard）",
            label="AI 难度",
            tag="ai",
            order=1,
        )
        use_llm_speech: bool = Field(
            default=True,
            description="AI 发言是否使用 LLM 生成",
            label="LLM 发言",
            tag="ai",
            order=2,
        )

    @config_section("narration", title="叙事风格", tag="narration", order=3)
    class NarrationSection(SectionBase):
        style: str = Field(
            default="standard",
            description="叙事风格（concise/standard/dramatic）",
            label="叙事风格",
            tag="narration",
            order=0,
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    game: GameSection = Field(default_factory=GameSection)
    ai: AISection = Field(default_factory=AISection)
    narration: NarrationSection = Field(default_factory=NarrationSection)
