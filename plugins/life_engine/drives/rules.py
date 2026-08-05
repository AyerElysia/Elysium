"""默认冲动规则集 — 基于现存可审计状态的行为建议。

重构说明（2026-07-31）：
- 移除依赖已删除 neuromod 子系统的规则
  (curiosity/sociability/energy/diligence/contentment)
- 改为基于当前可审计状态：思考流、好奇刺点、学习进展、
  河流回望、自主意向、紧急 todo
- 退役仅凭 active/open 线索周期性推动主体的旧规则
- 保持"建议而非命令"的设计哲学
"""
from __future__ import annotations

from .impulse import ImpulseRule


# ---- learning_reflect ----
def _learning_reflect_condition(neuromod_state: dict, context: dict) -> bool:
    """有学习进展时建议反思。"""
    return context.get("has_learning_progress", False)

learning_reflect = ImpulseRule(
    name="learning_reflect",
    condition=_learning_reflect_condition,
    suggestion="学习系统有新进展；可以看看新验证的领悟或技能目录",
    tools=["nucleus_skill"],
    cooldown_minutes=60,
)


# ---- river_consolidate ----
def _river_consolidate_condition(neuromod_state: dict, context: dict) -> bool:
    """有待沉淀的河流记忆时建议回望。"""
    return context.get("has_pending_river_moments", False)

river_consolidate = ImpulseRule(
    name="river_consolidate",
    condition=_river_consolidate_condition,
    suggestion="长河里积累了一些留痕；如果愿意，可以回望并写下它对你意味着什么",
    tools=["nucleus_write_narrative"],
    cooldown_minutes=120,
)


# ---- intent_review ----
def _intent_review_condition(neuromod_state: dict, context: dict) -> bool:
    """有自主意向时建议审视。"""
    return context.get("has_autonomy_intents", False)

intent_review = ImpulseRule(
    name="intent_review",
    condition=_intent_review_condition,
    suggestion="你登记了一些延迟意向；可以看看它们，决定是否调整或取消",
    tools=["nucleus_list_autonomy_intents"],
    cooldown_minutes=90,
)


# ---- todo_attend ----
def _todo_attend_condition(neuromod_state: dict, context: dict) -> bool:
    """有紧急/逾期 todo 时建议关注（但不是命令执行）。"""
    return context.get("has_urgent_todos", False)

todo_attend = ImpulseRule(
    name="todo_attend",
    condition=_todo_attend_condition,
    suggestion="有紧急或逾期的 TODO；这是承诺提醒，不是命令——可以观察、整理或释放",
    tools=["nucleus_list_todos"],
    cooldown_minutes=45,
)


DEFAULT_RULES: list[ImpulseRule] = [
    learning_reflect,
    river_consolidate,
    intent_review,
    todo_attend,
]
