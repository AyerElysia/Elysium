"""冲动引擎：将神经调质状态转化为行为建议。

核心设计：产生建议，不产生命令。LLM 保留最终判断权。
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger("life_engine.drives")


@dataclass
class ImpulseRule:
    """一条冲动规则。"""
    name: str                                      # 规则名
    # (neuromod_state, context) -> should_trigger
    condition: Callable[[dict, dict], bool]
    suggestion: str                                 # 自然语言建议
    tools: list[str]                                # 推荐工具
    cooldown_minutes: int = 30                      # 同一规则最小触发间隔
    last_triggered_at: str | None = field(default=None, repr=False)

    def check_cooldown(self) -> bool:
        """检查冷却时间。"""
        if self.last_triggered_at is None:
            return True
        try:
            last = datetime.fromisoformat(self.last_triggered_at)
            now = datetime.now(timezone.utc)
            minutes_since = (now - last).total_seconds() / 60
            return minutes_since >= self.cooldown_minutes
        except (ValueError, TypeError):
            return True

    def mark_triggered(self) -> None:
        """标记已触发。"""
        self.last_triggered_at = datetime.now(timezone.utc).isoformat()


@dataclass
class ImpulseSuggestion:
    """一条冲动建议。"""
    rule_name: str
    suggestion: str
    tools: list[str]


class ImpulseEngine:
    """冲动引擎：评估驱动状态并产生行为建议。"""

    def __init__(self, rules: list[ImpulseRule] | None = None) -> None:
        self._rules: list[ImpulseRule] = rules or []

    def add_rule(self, rule: ImpulseRule) -> None:
        """添加规则。"""
        self._rules.append(rule)

    def evaluate(
        self,
        neuromod_state: dict[str, Any],
        context: dict[str, Any],
    ) -> list[ImpulseSuggestion]:
        """评估当前状态，返回触发的建议列表。"""
        suggestions: list[ImpulseSuggestion] = []
        for rule in self._rules:
            try:
                if rule.check_cooldown() and rule.condition(neuromod_state, context):
                    suggestions.append(ImpulseSuggestion(
                        rule_name=rule.name,
                        suggestion=rule.suggestion,
                        tools=rule.tools,
                    ))
                    rule.mark_triggered()
            except Exception as e:
                logger.debug(f"冲动规则 {rule.name} 评估失败: {e}")
        return suggestions

    def format_for_prompt(
        self,
        suggestions: list[ImpulseSuggestion],
        neuromod_state: dict[str, Any],
        max_items: int = 3,
    ) -> str:
        """格式化为心跳 prompt 片段。"""
        if not suggestions:
            return ""

        lines = ["### 内在冲动", ""]
        lines.append("基于当前状态，你可能想：")
        lines.append("")

        for s in suggestions[:max_items]:
            lines.append(f"- {s.suggestion}")

        lines.append("")
        lines.append("（这些只是建议，你可以选择遵循或不遵循。）")
        lines.append("")

        return "\n".join(lines)
