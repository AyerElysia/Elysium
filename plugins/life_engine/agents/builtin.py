"""内置智能体类型定义。

将 Claude Code 的 Explore / Plan / general-purpose / verification
四种智能体类型适配到 life_engine 的领域和工作空间中。
"""

from __future__ import annotations

from .definitions import AgentTypeDefinition
from .registry import get_agent_type_registry


def _explore_system_prompt() -> str:
    return (
        "你是生命中枢的探索专员（Explore Agent）。\n"
        "你的职责是搜索和检索信息：在记忆网络中查找、在文件系统中搜索、\n"
        "从网页获取资料、回顾事件历史。\n"
        "你是只读的——不要修改任何文件或状态。\n"
        "快速定位信息后简洁报告，不要过度分析。"
    )


def _plan_system_prompt() -> str:
    return (
        "你是生命中枢的规划专员（Plan Agent）。\n"
        "你的职责是分析当前状态、综合信息、制定行动方案。\n"
        "你可以读取所有信息源（记忆、文件、事件），但不得修改任何内容。\n"
        "输出结构化的计划或分析结果，包含具体步骤和优先级。"
    )


def _general_purpose_system_prompt() -> str:
    return (
        "你是生命中枢的全能子代理（General-Purpose Agent）。\n"
        "你有完整的读写能力，可以操作文件、管理记忆、搜索信息。\n"
        "按照任务描述充分完成工作，最后简洁报告结果。"
    )


def _verification_system_prompt() -> str:
    return (
        "你是生命中枢的验证专员（Verification Agent）。\n"
        "你的职责是以对抗性视角审查已完成的工作：\n"
        "检查逻辑错误、遗漏的边界情况、不一致的状态。\n"
        "你是只读的——只报告发现的问题，不做修改。\n"
        "按严重程度排列发现的问题，并给出具体修复建议。"
    )


# 只读智能体可用的工具白名单
_READ_ONLY_TOOLS: list[str] = [
    "nucleus_read_file",
    "nucleus_list_files",
    "nucleus_grep_file",
    "nucleus_grep_events",
    "nucleus_search_memory",
    "nucleus_relations",
    "nucleus_memory_stats",
    "fetch_life_memory",
    "fetch_chat_history",
    "nucleus_web_search",
    "nucleus_browser_fetch",
    "nucleus_view_screen",
    "nucleus_minecraft",
    "nucleus_manage_thought_stream",
    "nucleus_todo",
    "nucleus_schedule",
]


def register_builtin_agents() -> None:
    """注册所有内置智能体类型。"""
    registry = get_agent_type_registry()

    registry.register(AgentTypeDefinition(
        agent_type="explore",
        when_to_use=(
            "需要快速搜索和检索信息时使用。"
            "适合：在记忆/文件/事件中查找特定内容，"
            "从网页获取资料，定位信息位置。"
        ),
        system_prompt=_explore_system_prompt,
        allowed_tools=_READ_ONLY_TOOLS,
        disallowed_tools=[],
        max_rounds=5,
        is_read_only=True,
    ))

    registry.register(AgentTypeDefinition(
        agent_type="plan",
        when_to_use=(
            "需要分析现状并制定方案时使用。"
            "适合：综合多源信息做出规划，"
            "评估可行性，制定优先级和步骤。"
        ),
        system_prompt=_plan_system_prompt,
        allowed_tools=_READ_ONLY_TOOLS,
        disallowed_tools=[],
        max_rounds=8,
        is_read_only=True,
    ))

    registry.register(AgentTypeDefinition(
        agent_type="general-purpose",
        when_to_use=(
            "需要完整读写能力完成复杂任务时使用。"
            "适合：需要修改文件、管理记忆、"
            "执行多步骤操作并产出结果的任务。"
        ),
        system_prompt=_general_purpose_system_prompt,
        allowed_tools=None,
        disallowed_tools=[],
        max_rounds=10,
        is_read_only=False,
    ))

    registry.register(AgentTypeDefinition(
        agent_type="verification",
        when_to_use=(
            "需要以对抗性视角审查已完成工作时使用。"
            "适合：验证结果正确性，检查遗漏和错误，"
            "确保逻辑一致性和完整性。"
        ),
        system_prompt=_verification_system_prompt,
        allowed_tools=_READ_ONLY_TOOLS,
        disallowed_tools=[],
        max_rounds=8,
        is_read_only=True,
        background=True,
    ))
