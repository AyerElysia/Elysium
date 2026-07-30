"""上下文桥接。

从 life_engine 读取运行态上下文（人格、记忆、WorldState 切片），
为全双工路径格式化为 session instructions，
为降级路径格式化为 chat system message。
"""

from __future__ import annotations

from typing import Any

from src.kernel.logger import get_logger

logger = get_logger("voice_live.context_bridge", display="Context Bridge")


class ContextBridge:
    """上下文桥接器。

    职责：
    - 从 life_engine 读取人格设定、运行态记忆、WorldState
    - 格式化为适合语音通话的 system prompt
    - 将通话事件写入统一事件流（可选）
    """

    def __init__(self, config: Any) -> None:
        self._config = config
        self._session_config = config.session if config else None

    async def build_system_prompt(self) -> str:
        """构建语音通话的系统提示词。

        组合：人格核心 + 运行态上下文 + 通话专属指令
        """
        parts: list[str] = []

        # 1. 通话基础指令
        parts.append(self._build_base_instructions())

        # 2. 人格与运行态上下文（如果配置启用）
        if self._session_config and self._session_config.include_life_runtime_context:
            runtime_ctx = await self._fetch_runtime_context()
            if runtime_ctx:
                parts.append(runtime_ctx)

        # 3. 最近事件流（如果配置启用）
        if self._session_config and self._session_config.include_unified_events:
            events_ctx = await self._fetch_recent_events()
            if events_ctx:
                parts.append(events_ctx)

        return "\n\n".join(parts)

    async def build_full_duplex_instructions(self) -> str:
        """为全双工 Provider 构建 instructions。

        全双工模型的 instructions 需要更简洁，
        因为它在实时对话中持续生效。
        """
        parts: list[str] = []

        # 简洁人格描述
        persona = await self._fetch_persona_brief()
        if persona:
            parts.append(persona)

        # 通话行为指令
        parts.append(
            "你正在进行实时语音通话。请：\n"
            "- 用自然口语化的方式说话，避免书面语\n"
            "- 回复简短有力，每次不超过2-3句\n"
            "- 可以使用语气词（嗯、啊、哈）让对话更自然\n"
            "- 如果对方沉默，可以主动找话题\n"
            "- 不要使用 markdown 格式或列表"
        )

        return "\n\n".join(parts)

    async def record_event(self, event_type: str, content: str) -> None:
        """将通话事件写入统一事件流。"""
        if not self._session_config or not self._session_config.record_to_life:
            return

        try:
            from src.core.managers import get_plugin_manager

            plugin = get_plugin_manager().get_plugin("life_engine")
            if not plugin:
                return

            event_service = getattr(plugin, "event_service", None)
            if event_service and hasattr(event_service, "append_event"):
                stream_id = self._session_config.stream_id
                stream_name = self._session_config.stream_name
                await event_service.append_event(
                    stream_id=stream_id,
                    stream_name=stream_name,
                    event_type=event_type,
                    content=content,
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"事件记录失败: {exc}")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _build_base_instructions(self) -> str:
        """构建基础通话指令。"""
        user_name = self._session_config.user_name if self._session_config else "用户"
        return (
            f"你正在和{user_name}进行实时语音通话。\n"
            "请保持自然、亲切的说话方式，像朋友之间打电话一样。\n"
            "回复要简短口语化，避免长段落和书面格式。"
        )

    async def _fetch_runtime_context(self) -> str:
        """从 life_engine 获取运行态上下文。"""
        try:
            from src.core.managers import get_plugin_manager

            plugin = get_plugin_manager().get_plugin("life_engine")
            if not plugin:
                return ""

            parts: list[str] = []

            # 人格设定
            persona = getattr(plugin, "persona_text", None)
            if persona and isinstance(persona, str):
                # 截取前 800 字符避免过长
                parts.append(f"[人格]\n{persona[:800]}")

            # WorldState 感知切片
            world_state = getattr(plugin, "world_state", None)
            if world_state and hasattr(world_state, "render_for_perception"):
                from plugins.life_engine.service.world_state import PerceptionFilter
                pf = PerceptionFilter(
                    relationship_ids=[],
                    scene_ids=[],
                    thread_kinds=["topic"],
                    include_body_state=True,
                    include_commitments=False,
                )
                perception = world_state.render_for_perception(pf, max_chars=1000)
                if perception:
                    parts.append(f"[当前状态]\n{perception}")

            return "\n".join(parts)

        except Exception as exc:  # noqa: BLE001
            logger.debug(f"运行态上下文获取失败: {exc}")
            return ""

    async def _fetch_persona_brief(self) -> str:
        """获取简洁人格描述。"""
        try:
            from src.core.managers import get_plugin_manager

            plugin = get_plugin_manager().get_plugin("life_engine")
            if not plugin:
                return ""

            persona = getattr(plugin, "persona_text", None)
            if persona and isinstance(persona, str):
                return f"[你的人格设定]\n{persona[:500]}"
            return ""
        except Exception:  # noqa: BLE001
            return ""

    async def _fetch_recent_events(self) -> str:
        """获取最近统一事件流。"""
        try:
            from src.core.managers import get_plugin_manager

            plugin = get_plugin_manager().get_plugin("life_engine")
            if not plugin:
                return ""

            event_service = getattr(plugin, "event_service", None)
            if not event_service or not hasattr(event_service, "get_recent_events"):
                return ""

            events = await event_service.get_recent_events(limit=5)
            if not events:
                return ""

            lines = ["[最近发生的事]"]
            for evt in events:
                if isinstance(evt, dict):
                    lines.append(f"- {evt.get('content', '')[:100]}")
                else:
                    lines.append(f"- {str(evt)[:100]}")
            return "\n".join(lines)

        except Exception as exc:  # noqa: BLE001
            logger.debug(f"事件流获取失败: {exc}")
            return ""
