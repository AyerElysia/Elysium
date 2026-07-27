"""Thinking Plugin - 思考动作插件主模块。

提供 think action，让爱莉希雅在发送回复前补一段可见的内心动作。
"""

from src.core.components.base import BasePlugin
from src.core.components.loader import register_plugin

from .actions.think_action import ThinkAction
from .thinker_trigger import ThinkerTrigger
from .config import ThinkingConfig

LEGACY_THINKING_HABIT_MARKERS = (
    "think 工具",
    "当你不调用 think 时，说明你已经想清楚了，可以直接回复用户了。",
)

ACTION_THINKING_HABIT = """# 思考的习惯
在组织回复时，先把你的内心活动整理清楚，再发送给用户。

你现在的思考动作规则是：
- 当你准备回复用户时，先调用 `action-think`
- 然后在同一轮调用 `action-send_text`
- `thought` 只写你的内心活动、分析和取舍
- 真正要发给用户的话，必须单独写进 `action-send_text.content`

思考时可以快速确认：
- 用户现在真正想要的是什么？
- 我是不是遗漏了上下文？
- 我的回答重点应该先说什么？
- 我此刻的情绪和语气应该是什么？
- 我决定怎么回答，以及预期对方会怎么接话？
"""

THINKING_REMINDER_NAME = "thinking_contract"
LEGACY_REMINDER_NAMES = (
    "thinking_habit",
    "thinking_fields",
    "thinking_must_think",
)


@register_plugin
class ThinkingPlugin(BasePlugin):
    """思考动作插件。

    提供 think action，让爱莉希雅在发送回复前同步记录一段思考动作。
    该动作不再承担 follow-up 推理，只作为发送回复前的强制前置动作。

    Attributes:
        plugin_name: 插件名称
        plugin_version: 插件版本
        plugin_author: 插件作者
        plugin_description: 插件功能描述
    """

    plugin_name = "thinking_plugin"
    plugin_version = "1.0.0"
    plugin_author = "Neo-MoFox Team"
    plugin_description = "思考动作插件 - 让爱莉希雅在发送回复前展现内心活动"
    configs = [ThinkingConfig]

    def get_components(self) -> list[type]:
        """获取插件内所有组件类。

        Returns:
            list[type]: 包含插件内所有组件类的列表
        """
        return [ThinkAction, ThinkerTrigger]

    async def on_plugin_loaded(self) -> None:
        """插件加载时的初始化钩子。

        从配置文件读取思考习惯引导词和字段开关配置，注册到 system reminder。
        """
        from src.core.prompt import get_system_reminder_store

        config = self.config
        store = get_system_reminder_store()

        for legacy_name in LEGACY_REMINDER_NAMES:
            store.delete("actor", legacy_name)

        merged_reminder = self._build_actor_reminder(config)
        if merged_reminder:
            store.set(
                bucket="actor",
                name=THINKING_REMINDER_NAME,
                content=merged_reminder,
            )
        else:
            store.delete("actor", THINKING_REMINDER_NAME)

    def _build_must_think_reminder(self) -> str:
        """构建 think action 的强制提醒。"""
        return """# 强制规则：回复时必须同时使用 think action

**当你准备调用 `action-send_text` 给用户发送回复时，你必须在同一轮里先调用 `action-think`。**

硬性要求：
1. `action-think` 与 `action-send_text` 必须同时出现在同一轮调用列表中。
2. `action-think` 必须排在 `action-send_text` 之前。
3. 不允许直接只调用 `action-send_text`。
4. 不允许单独调用 `action-think` 后挂起等待。
5. `thought` 里只写你的内心活动与分析，不要把最终要发给用户的正文只写在这里。
6. 真正发送给用户的话，必须写进 `action-send_text.content`，而且 `content` 不能为空。
7. `action-think` 的参数中不允许出现 `content` 字段；`content` 只属于 `action-send_text`。

可以把 `action-think` 理解为“发送回复前，必须先补一段内心动作”。
"""

    def _build_actor_reminder(self, config: ThinkingConfig) -> str:
        """构建稳定注入到 actor bucket 的思考契约。"""
        parts = [
            self._resolve_thinking_habit_prompt(config.prompt.thinking_habit),
            self._build_fields_reminder(config),
            self._build_must_think_reminder(),
        ]
        merged = "\n\n".join(part.strip() for part in parts if str(part or "").strip())
        return merged.strip()

    def _resolve_thinking_habit_prompt(self, prompt_text: str) -> str:
        """兼容旧版 think 工具提示词，避免与 action 版规则冲突。"""
        normalized = str(prompt_text or "").strip()
        if not normalized:
            return ""

        if all(marker in normalized for marker in LEGACY_THINKING_HABIT_MARKERS):
            return ACTION_THINKING_HABIT

        return normalized

    def _build_fields_reminder(self, config: ThinkingConfig) -> str:
        """根据字段开关配置构建提示词。

        Args:
            config: 思考动作配置

        Returns:
            str: 字段使用提示词，如无需添加则返回空字符串
        """
        fields = config.fields
        parts = []

        if fields.enable_mood:
            parts.append(
                "- **mood（心情）**：请填写你此刻的情绪状态，如'开心'、'疑惑'、'担心'等。"
            )
        if fields.enable_decision:
            parts.append(
                "- **decision（决定）**：请填写你决定的下一步行动，如'去查日记'、'再分析一下'、'直接回复'等。"
            )
        if fields.enable_expected_response:
            parts.append(
                "- **expected_response（预期反应）**：请填写你预期用户看到回复后的反应，如'应该会满意'、'可能会追问'等。"
            )

        if not parts:
            return ""

        return "# 思考动作的字段要求\n\n调用 `action-think` 时，除了 `thought` 必填外，以下字段已启用，且为必填项，请务必填写：\n\n" + "\n".join(parts) + "\n\n这些字段现在为必填项，不应留空；若确实无法判断，请填写 \"未知\" 或简短说明原因。"
