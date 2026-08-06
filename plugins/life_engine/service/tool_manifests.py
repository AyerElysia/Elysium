"""Consciousness instance tool manifests.

Each consciousness type declares which tools it needs. When an instance is
created, only the tools in its manifest are injected into the LLM request.

Design principles:
- Manifests are advisory: she can access additional capabilities via the skill
  system's progressive disclosure (boundary reminder pattern).
- Tools NOT in the manifest are simply not injected as LLM tool schemas,
  saving context budget on every turn.
- Adding a new consciousness type only requires adding a manifest entry here.
- The nucleus (heartbeat/subconscious) tool registration is NOT affected by
  these manifests — it uses its own `life_engine_internal` channel.
"""

from __future__ import annotations

# Tool name format: "action-{name}" for actions, "tool-{name}" for tools.
# These match the names as they appear in the LLM tool list.

CONSCIOUSNESS_TOOL_MANIFESTS: dict[str, list[str]] = {
    # 记忆见证意识只读取经历账本，不注入聊天或行动工具。
    "memory_witness": [],
    # 全局聊天意识：私聊、群聊、所有日常对话
    "chat": [
        "action-life_send_text",
        "action-life_send_image",
        "action-life_send_voice",
        "action-life_pass_and_wait",
        "action-report_state",
        "action-record_inner_monologue",
        "tool-inner_dialogue",
        "tool-inner_query",
        "tool-conversation_evidence",
        "tool-recognize_voice",
        "tool-nucleus_save_media",
        "tool-nucleus_grep_events",
        "tool-nucleus_search_memory",
        "tool-nucleus_view_relations",
        "tool-nucleus_memory_stats",
        "action-send_emoji_meme",
        "tool-platform_action",
    ],
    # 我的世界意识：具身交互，纯视觉→键鼠
    "minecraft": [
        "tool-nucleus_minecraft",
        "action-life_send_text",
        "action-report_state",
    ],
    # 直播意识：弹幕互动，跨场景感知
    "livestream": [
        "action-life_send_text",
        "action-report_state",
        "tool-inner_query",
        "tool-conversation_evidence",
    ],
    # 语音通话意识：实时语音交互，跨场景感知
    "voice_live": [
        "action-report_state",     # 报告通话状态到 WorldState
        "tool-inner_query",        # 向潜意识查询
        "tool-conversation_evidence", # 有界查阅对话证据
    ],
}

def get_tool_manifest(kind: str) -> list[str]:
    """Get the tool manifest for a consciousness instance kind.

    Unknown kinds must declare their capabilities explicitly.  Silently
    inheriting chat powers would blur an instance boundary.
    """

    normalized = str(kind or "").strip()
    if normalized not in CONSCIOUSNESS_TOOL_MANIFESTS:
        raise KeyError(
            f"consciousness tool manifest is not declared: {normalized!r}"
        )
    return list(CONSCIOUSNESS_TOOL_MANIFESTS[normalized])


def is_tool_in_manifest(tool_name: str, kind: str) -> bool:
    """Check if a tool (by its LLM-visible name) is in the manifest for a kind.

    tool_name should be in the format "action-xxx" or "tool-xxx".
    """
    manifest = get_tool_manifest(kind)
    return tool_name in manifest
