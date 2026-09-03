"""Consciousness instance tool manifests.

Each consciousness type declares which tools it needs. When an instance is
created, only the tools in its manifest are injected into the LLM request.

Design principles:
- Manifests are advisory: she can access additional capabilities via the skill
  system's progressive disclosure (boundary reminder pattern).
- Tools NOT in the manifest are simply not injected as LLM tool schemas,
  saving context budget on every turn.
- Adding a new consciousness type only requires adding a manifest entry here.
- Heartbeat uses HEARTBEAT_TOOL_NAMES (life_engine_internal), not the chat
  kind lists below. Missing schema is not a subject refusal.
"""

from __future__ import annotations

from typing import Any

# Tool name format: "action-{name}" for actions, "tool-{name}" for tools.
# These match the names as they appear in the LLM tool list.

CONSCIOUSNESS_TOOL_MANIFESTS: dict[str, list[str]] = {
    # 记忆见证意识只读取经历账本，不注入聊天或行动工具。
    "memory_witness": [],
    # 全局聊天意识：私聊、群聊、所有日常对话。
    # nucleus_minecraft 保留在聊天清单里：进入共享世界的邀请常在聊天中发生，
    # 她必须能在对话里自主调用进入工具，而不是等别人带她。
    "chat": [
        "action-life_send_text",
        "action-life_send_file",
        "action-life_send_image",
        "action-life_send_voice",
        "action-life_pass_and_wait",
        "action-author_self_continuity_checkpoint",
        "action-report_state",
        "action-record_inner_monologue",
        "tool-inner_dialogue",
        "tool-inner_query",
        "tool-conversation_evidence",
        "tool-read_context_group",
        "tool-recognize_voice",
        "tool-nucleus_save_media",
        "tool-nucleus_grep_events",
        "tool-nucleus_search_memory",
        "tool-nucleus_read_memory_boundary",
        "tool-nucleus_memory_continuity_review",
        "tool-nucleus_relations",
        "tool-nucleus_memory_stats",
        "action-send_emoji_meme",
        "tool-platform_action",
        "tool-nucleus_minecraft",
        "tool-nucleus_proactive_query",
        "tool-nucleus_proactive_command",
        # 基础能力（文件/终端/屏幕/任务/日程/联网/子代理/媒体）：
        # 与 chatter._build_primary_tool_guide 静态文案对齐——文案列出的工具必须
        # 在 chat manifest 里，否则模型照文案调用会被过滤成"未知的工具"
        # （真实日志：tool-nucleus_grep_file / tool-nucleus_bash，2026-08-12）。
        "tool-inspect_media",
        "tool-life_run_agent",
        "tool-nucleus_bash",
        "tool-nucleus_browser_fetch",
        "tool-nucleus_web_search",
        "tool-nucleus_download",
        "tool-nucleus_edit_file",
        "tool-nucleus_apply_patch",
        "tool-nucleus_glob_file",
        "tool-nucleus_grep_file",
        "tool-nucleus_list_files",
        "tool-nucleus_todo",
        "tool-nucleus_mkdir",
        "tool-nucleus_read_file",
        "tool-nucleus_run_agent",
        "tool-nucleus_view_screen",
        "tool-nucleus_write_file",
    ],
    # 我的世界意识：具身交互，纯视觉→键鼠
    "minecraft": [
        "tool-nucleus_minecraft",
        "tool-nucleus_proactive_query",
        "tool-nucleus_proactive_command",
        "action-life_send_text",
        "action-report_state",
    ],
    # 直播意识：弹幕互动，跨场景感知
    "livestream": [
        "tool-nucleus_proactive_query",
        "tool-nucleus_proactive_command",
        "action-life_send_text",
        "action-report_state",
        "tool-inner_query",
        "tool-conversation_evidence",
    ],
    # 语音通话意识：实时语音交互，跨场景感知
    "voice_live": [
        "tool-nucleus_proactive_query",
        "tool-nucleus_proactive_command",
        "action-report_state",  # 报告通话状态到 WorldState
        "tool-inner_query",  # 向潜意识查询
        "tool-conversation_evidence",  # 有界查阅对话证据
    ],
}


def get_tool_manifest(kind: str) -> list[str]:
    """Get the tool manifest for a consciousness instance kind.

    Unknown kinds must declare their capabilities explicitly.  Silently
    inheriting chat powers would blur an instance boundary.
    """

    normalized = str(kind or "").strip()
    if normalized not in CONSCIOUSNESS_TOOL_MANIFESTS:
        raise KeyError(f"consciousness tool manifest is not declared: {normalized!r}")
    return list(CONSCIOUSNESS_TOOL_MANIFESTS[normalized])


def is_tool_in_manifest(tool_name: str, kind: str) -> bool:
    """Check if a tool (by its LLM-visible name) is in the manifest for a kind.

    tool_name should be in the format "action-xxx" or "tool-xxx".
    """
    manifest = get_tool_manifest(kind)
    return tool_name in manifest


# Heartbeat resident schemas. Invitation how-to is either a skill document
# (learning) or one of these already-resident tools (file_care / narrative /
# MEMORY continuity). Names are tool_name, not the LLM `tool-` prefix.
HEARTBEAT_TOOL_NAMES: tuple[str, ...] = (
    "nucleus_rest_heartbeat",
    "nucleus_read_file",
    "nucleus_write_file",
    "nucleus_edit_file",
    "nucleus_apply_patch",
    "nucleus_glob_file",
    "nucleus_list_files",
    "nucleus_mkdir",
    "nucleus_grep_file",
    "nucleus_grep_events",
    "nucleus_read_event",
    "nucleus_search_memory",
    "nucleus_read_memory_boundary",
    "nucleus_memory_continuity_review",
    "nucleus_proactive_query",
    "nucleus_proactive_command",
    "nucleus_todo",
    "nucleus_schedule",
    "nucleus_skill",
    "nucleus_learn",
    "nucleus_write_narrative",
    "author_self_continuity_checkpoint",
    "read_context_group",
)

LIFE_ENGINE_INTERNAL_MANIFEST: tuple[str, ...] = tuple(
    (
        f"action-{name}"
        if name == "author_self_continuity_checkpoint"
        else f"tool-{name}"
    )
    for name in HEARTBEAT_TOOL_NAMES
)


def heartbeat_tool_classes() -> list[type[Any]]:
    """Resolve HEARTBEAT_TOOL_NAMES to tool classes. Import is lazy."""

    from ..core.context_stewardship import (
        LifeAuthorSelfContinuityCheckpointAction,
        LifeReadContextGroupTool,
    )
    from ..learning.learn_tool import NucleusLearnTool
    from ..memory.boundary_tools import LifeReadMemoryBoundaryTool
    from ..memory.continuity_tools import LifeMemoryContinuityReviewSessionTool
    from ..memory.tools import LifeEngineSearchMemoryTool
    from ..narrative.tools import LifeEngineWriteNarrativeTool
    from ..proactive.tools import (
        LifeEngineProactiveCommandTool,
        LifeEngineProactiveQueryTool,
    )
    from ..tools.event_grep_tools import (
        LifeEngineGrepEventsTool,
        LifeEngineReadEventTool,
    )
    from ..tools.file_tools import (
        LifeEngineApplyPatchTool,
        LifeEngineEditFileTool,
        LifeEngineGlobFileTool,
        LifeEngineListFilesTool,
        LifeEngineMakeDirectoryTool,
        LifeEngineReadFileTool,
        LifeEngineWriteFileTool,
    )
    from ..tools.grep_tools import LifeEngineGrepFileTool
    from ..tools.rest_tools import LifeEngineRestHeartbeatTool
    from ..tools.schedule_tools import NucleusScheduleTool
    from ..tools.skill_tools import LifeEngineSkillTool
    from ..tools.todo_tools import NucleusTodoTool

    classes: tuple[type[Any], ...] = (
        LifeEngineRestHeartbeatTool,
        LifeEngineReadFileTool,
        LifeEngineWriteFileTool,
        LifeEngineEditFileTool,
        LifeEngineApplyPatchTool,
        LifeEngineGlobFileTool,
        LifeEngineListFilesTool,
        LifeEngineMakeDirectoryTool,
        LifeEngineGrepFileTool,
        LifeEngineGrepEventsTool,
        LifeEngineReadEventTool,
        LifeEngineSearchMemoryTool,
        LifeReadMemoryBoundaryTool,
        LifeMemoryContinuityReviewSessionTool,
        LifeEngineProactiveQueryTool,
        LifeEngineProactiveCommandTool,
        NucleusTodoTool,
        NucleusScheduleTool,
        LifeEngineSkillTool,
        NucleusLearnTool,
        LifeEngineWriteNarrativeTool,
        LifeAuthorSelfContinuityCheckpointAction,
        LifeReadContextGroupTool,
    )
    by_name: dict[str, type[Any]] = {}
    for cls in classes:
        name = str(getattr(cls, "tool_name", "") or getattr(cls, "action_name", "") or "")
        if name:
            by_name[name] = cls
    missing = [name for name in HEARTBEAT_TOOL_NAMES if name not in by_name]
    extra = sorted(set(by_name) - set(HEARTBEAT_TOOL_NAMES))
    if missing or extra:
        raise RuntimeError(
            "HeartbeatToolManifestMismatch: "
            f"missing={missing} extra={extra}"
        )
    return [by_name[name] for name in HEARTBEAT_TOOL_NAMES]
