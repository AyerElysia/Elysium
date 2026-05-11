"""默认冲动规则集。"""
from __future__ import annotations
from typing import Any
from .impulse import ImpulseRule


def _get_modulator_value(neuromod_state: dict[str, Any], name: str) -> float:
    """从神经调质状态中提取指定调质的浓度值。"""
    mod = neuromod_state.get(name, {})
    if isinstance(mod, dict):
        return mod.get("value", 0.5)
    if isinstance(mod, (int, float)):
        return float(mod)
    return 0.5


# ---- curiosity_explore ----
def _curiosity_explore_condition(neuromod_state: dict, context: dict) -> bool:
    curiosity = _get_modulator_value(neuromod_state, "curiosity")
    idle = context.get("idle_heartbeats", 0)
    return curiosity > 0.65 and idle >= 2

curiosity_explore = ImpulseRule(
    name="curiosity_explore",
    condition=_curiosity_explore_condition,
    suggestion="你的好奇心正盛，有没有感兴趣的话题想深入了解？",
    tools=["nucleus_web_search", "nucleus_manage_thought_stream"],
    cooldown_minutes=45,
)

# ---- social_reach_out ----
def _social_reach_out_condition(neuromod_state: dict, context: dict) -> bool:
    sociability = _get_modulator_value(neuromod_state, "sociability")
    silence = context.get("silence_minutes", 0)
    return sociability > 0.6 and silence > 30

social_reach_out = ImpulseRule(
    name="social_reach_out",
    condition=_social_reach_out_condition,
    suggestion="你很想和大家说说话；先看看是否真的有表达层缺失的关键背景，没有就把这股社交欲写进思考流",
    tools=["nucleus_manage_thought_stream"],
    cooldown_minutes=45,
)

# ---- diligence_todo ----
def _diligence_todo_condition(neuromod_state: dict, context: dict) -> bool:
    diligence = _get_modulator_value(neuromod_state, "diligence")
    has_todos = context.get("has_urgent_todos", False)
    return diligence > 0.65 and has_todos

diligence_todo = ImpulseRule(
    name="diligence_todo",
    condition=_diligence_todo_condition,
    suggestion="你的专注力很好，正好可以推进待办事项",
    tools=["nucleus_list_todos"],
    cooldown_minutes=30,
)

# ---- break_silence ----
def _break_silence_condition(neuromod_state: dict, context: dict) -> bool:
    silence = context.get("silence_minutes", 0)
    energy = _get_modulator_value(neuromod_state, "energy")
    return silence > 60 and energy > 0.5

break_silence = ImpulseRule(
    name="break_silence",
    condition=_break_silence_condition,
    suggestion="安静很久了；可以推进思考流，只有发现明确的信息差时才补充给表达层",
    tools=["nucleus_manage_thought_stream"],
    cooldown_minutes=60,
)

# ---- thought_pursue ----
def _thought_pursue_condition(neuromod_state: dict, context: dict) -> bool:
    has_thoughts = context.get("has_active_thoughts", False)
    idle = context.get("idle_heartbeats", 0)
    return has_thoughts and idle >= 1

thought_pursue = ImpulseRule(
    name="thought_pursue",
    condition=_thought_pursue_condition,
    suggestion="你有未完成的思考，也许可以继续深入",
    tools=["nucleus_manage_thought_stream"],
    cooldown_minutes=20,
)

# ---- rest_well ----
def _rest_well_condition(neuromod_state: dict, context: dict) -> bool:
    energy = _get_modulator_value(neuromod_state, "energy")
    contentment = _get_modulator_value(neuromod_state, "contentment")
    idle = context.get("idle_heartbeats", 0)
    # 精力低且满足感合理时，休息是自然的选择
    return energy < 0.4 and contentment > 0.3 and idle >= 3

rest_well = ImpulseRule(
    name="rest_well",
    condition=_rest_well_condition,
    suggestion="你的精力需要恢复，安静休息是现在最好的选择",
    tools=[],
    cooldown_minutes=120,
)


DEFAULT_RULES: list[ImpulseRule] = [
    curiosity_explore,
    social_reach_out,
    diligence_todo,
    break_silence,
    thought_pursue,
    rest_well,
]
