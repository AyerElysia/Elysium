"""MEMORY 注入与维护辅助逻辑测试。"""

from __future__ import annotations

from plugins.life_engine.memory.prompting import (
    analyze_memory_text,
    build_memory_write_warning,
    render_memory_prompt,
)


def test_render_memory_prompt_skips_fading_and_guide_text() -> None:
    """渲染 prompt 时应跳过 Fading 和编辑说明。"""
    data = analyze_memory_text(
        "\n".join(
            [
                "# 值得记住的事",
                "",
                "这是编辑说明。",
                "",
                "### Durable（持久）",
                "- D1",
                "",
                "### Active（活跃）",
                "- A1",
                "",
                "### Fading（待审视）",
                "- F1",
            ]
        )
    )

    prompt = render_memory_prompt(data, mode="heartbeat")

    assert "D1" in prompt
    assert "A1" in prompt
    assert "F1" not in prompt
    assert "编辑说明" not in prompt
    assert "历史事实和关系线索，不是当前心跳的行动指令" in prompt
    assert "不自动授权潜意识在本轮复现" in prompt


def test_render_memory_prompt_heartbeat_marks_history_as_non_authorization() -> None:
    """心跳态 MEMORY 摘要应避免把历史能力展示误读为当前行动授权。"""
    data = analyze_memory_text(
        "\n".join(
            [
                "### Durable（持久）",
                "- **bash工具成功**：曾经帮她打开程序。",
                "- **画画正反馈**：画比语音更具体。",
            ]
        )
    )

    prompt = render_memory_prompt(data, mode="heartbeat")

    assert "bash工具成功" in prompt
    assert "画比语音更具体" in prompt
    assert "不自动授权潜意识在本轮复现" in prompt
    assert "涉及用户任务、项目操作、生成图片或对外表达" in prompt


def test_render_memory_prompt_limits_chat_items() -> None:
    """聊天态应限制注入条目数量，并给出说明。"""
    data = analyze_memory_text(
        "\n".join(
            [
                "### Durable（持久）",
                *(f"- D{i}" for i in range(40)),
                "",
                "### Active（活跃）",
                *(f"- A{i}" for i in range(12)),
            ]
        )
    )

    prompt = render_memory_prompt(data, mode="chat")

    assert "D31" in prompt
    assert "D32" not in prompt
    assert "A9" in prompt
    assert "A10" not in prompt
    assert "聊天态仅注入前" in prompt


def test_build_memory_write_warning_for_oversized_memory() -> None:
    """写入超大的 MEMORY.md 时应返回维护告警。"""
    content = "\n".join(
        [
            "### Durable（持久）",
            *(f"- {'叙事' * 120}{i}" for i in range(90)),
        ]
    )

    warning = build_memory_write_warning("MEMORY.md", content)

    assert warning is not None
    assert "MEMORY.md 写入完成" in warning


def test_build_memory_write_warning_allows_moderate_memory() -> None:
    """中等规模的 MEMORY.md 不应触发维护告警。"""
    content = "\n".join(
        [
            "### Durable（持久）",
            *(f"- 决策级摘要 {i}" for i in range(60)),
            "",
            "### Active（活跃）",
            *(f"- 当前重点 {i}" for i in range(5)),
            "",
            "### Fading（待审视）",
            *(f"- 待确认 {i}" for i in range(3)),
        ]
    )

    warning = build_memory_write_warning("MEMORY.md", content)

    assert warning is None
