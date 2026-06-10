"""心跳 prompt 注入段落的统一协议（SectionProvider）。

可言说法则：**任何子系统向心跳注入文本，都实现同一个 SectionProvider 接口；
装配方只做循环。** 新增子系统注入 = 新增一个 provider，不再修改装配代码。

每个 provider 自己持有"是否注入"的配置判断（enabled）与渲染逻辑（render），
一个 provider 渲染失败不影响其他段落。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from src.app.plugin_system.api import log_api

logger = log_api.get_logger("life_engine.prompt_sections")


@dataclass
class SectionContext:
    """一次心跳渲染所需的共享上下文。"""

    service: Any
    config: Any
    today_str: str
    silence_minutes: int | None = None
    idle_heartbeats: int = 0


class HeartbeatSectionProvider(ABC):
    """心跳注入段落的统一接口。"""

    section_id: str = ""

    def enabled(self, ctx: SectionContext) -> bool:
        return True

    @abstractmethod
    async def render(self, ctx: SectionContext) -> str | None:
        """渲染本段落文本；返回 None 或空串表示本次无内容。"""


async def render_heartbeat_sections(
    providers: list[HeartbeatSectionProvider],
    ctx: SectionContext,
) -> list[str]:
    """按注册顺序渲染所有段落，单段失败不拖垮整体。"""
    texts: list[str] = []
    for provider in providers:
        try:
            if not provider.enabled(ctx):
                continue
            text = await provider.render(ctx)
            if text and text.strip():
                texts.append(text)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"prompt 段落 '{provider.section_id}' 渲染失败: {exc}")
    return texts


# ============================================================
# 内置 provider
# ============================================================


class InnerStateSection(HeartbeatSectionProvider):
    """神经调质 + 习惯的内在状态摘要。"""

    section_id = "inner_state"

    def enabled(self, ctx: SectionContext) -> bool:
        neuromod_cfg = getattr(ctx.config, "neuromod", None)
        if getattr(ctx.service, "_inner_state", None) is None or neuromod_cfg is None:
            return False
        return bool(neuromod_cfg.enabled) and bool(neuromod_cfg.inject_to_heartbeat)

    async def render(self, ctx: SectionContext) -> str | None:
        return ctx.service._inner_state.format_full_state_for_prompt(ctx.today_str)


class ThoughtStreamsSection(HeartbeatSectionProvider):
    """当前活跃思考流（heartbeat 内不分组、不做 delta）。"""

    section_id = "thought_streams"

    def enabled(self, ctx: SectionContext) -> bool:
        if getattr(ctx.service, "_thought_manager", None) is None:
            return False
        streams_cfg = getattr(ctx.config, "streams", None)
        return streams_cfg is None or bool(getattr(streams_cfg, "inject_to_heartbeat", True))

    async def render(self, ctx: SectionContext) -> str | None:
        streams_cfg = getattr(ctx.config, "streams", None)
        focus_window = (
            int(getattr(streams_cfg, "focus_window_minutes", 30) or 30) if streams_cfg else 30
        )
        body = ctx.service._thought_manager.format_for_prompt(
            max_items=3,
            focus_window_minutes=focus_window,
            grouped=False,
            mark_delta=False,
        )
        if not body:
            return None
        return f"### 当前思考流\n{body}"


class CuriositySection(HeartbeatSectionProvider):
    """异步好奇层留下的刺点（含承接引导），由心跳态自行决定是否承接。"""

    section_id = "curiosity"

    def enabled(self, ctx: SectionContext) -> bool:
        curiosity_cfg = getattr(ctx.config, "curiosity", None)
        if curiosity_cfg is None:
            return True
        return bool(getattr(curiosity_cfg, "enabled", True)) and bool(
            getattr(curiosity_cfg, "inject_to_heartbeat", True)
        )

    async def render(self, ctx: SectionContext) -> str | None:
        from ..curiosity import format_curiosity_signal

        signal = await ctx.service._get_curiosity_engine().load_signal()
        body = format_curiosity_signal(signal)
        if not body:
            return None
        guidance = (
            "如果这个刺点你确实在意，可以靠近它（联想、检索、轻轻观察），"
            "或者用 `nucleus_manage_thought_stream`（action=create，absorb_curiosity=true）"
            "开一条思考流把它留住——承接之后这股牵引会自然放下。\n"
            "如果你觉得它已经闭合、或并不重要，放下它也很好。"
        )
        return f"{body}\n\n{guidance}"


class ImpulseSection(HeartbeatSectionProvider):
    """冲动引擎的建议（纯建议，可遵循可忽略）。"""

    section_id = "impulses"

    def enabled(self, ctx: SectionContext) -> bool:
        if getattr(ctx.service, "_impulse_engine", None) is None:
            return False
        drives_cfg = getattr(ctx.config, "drives", None)
        return drives_cfg is None or bool(getattr(drives_cfg, "inject_to_heartbeat", True))

    async def render(self, ctx: SectionContext) -> str | None:
        service = ctx.service
        neuromod_state: dict[str, Any] = {}
        if getattr(service, "_inner_state", None) is not None:
            try:
                neuromod_state = service._inner_state.get_full_state()
            except Exception:  # noqa: BLE001
                pass
        has_urgent_todos = False
        try:
            from ..tools.todo_tools import TodoStorage

            active_todos = [
                todo
                for todo in TodoStorage(service._workspace_dir()).load()
                if todo.status not in {"completed", "cancelled", "archived"}
            ]
            has_urgent_todos = any(
                todo.priority == "urgent" or todo.is_overdue() or todo.needs_review()
                for todo in active_todos
            )
        except Exception:  # noqa: BLE001
            has_urgent_todos = False
        context = {
            "silence_minutes": ctx.silence_minutes or 0,
            "idle_heartbeats": ctx.idle_heartbeats,
            "has_active_thoughts": bool(
                getattr(service, "_thought_manager", None)
                and service._thought_manager.list_active()
            ),
            "has_urgent_todos": has_urgent_todos,
        }
        suggestions = service._impulse_engine.evaluate(neuromod_state, context)
        return service._impulse_engine.format_for_prompt(
            suggestions, neuromod_state, max_items=3
        )


class SendTargetsSection(HeartbeatSectionProvider):
    """她可以触达的人和地方——主动性的行动空间。

    主动性因果链中"空间"一环：意图需要知道可以落向何处。
    只呈现地图，不催促出发。
    """

    section_id = "send_targets"

    def enabled(self, ctx: SectionContext) -> bool:
        autonomy_cfg = getattr(ctx.config, "autonomy", None)
        if autonomy_cfg is None:
            return True
        return bool(getattr(autonomy_cfg, "enabled", True)) and bool(
            getattr(autonomy_cfg, "show_targets_in_heartbeat", True)
        )

    async def render(self, ctx: SectionContext) -> str | None:
        from ..core.send_targets import list_recent_send_targets

        runtime_cfg = getattr(ctx.config, "runtime_sync", None)
        targets = await list_recent_send_targets(
            current_stream_id="",
            limit=int(getattr(runtime_cfg, "send_targets_limit", 8) or 8),
            active_window_hours=float(
                getattr(runtime_cfg, "send_targets_window_hours", 24.0) or 24.0
            ),
        )
        if not targets:
            return None
        lines = ["### 你可以触达的人和地方", ""]
        for target in targets:
            chat_label = "群聊" if target.chat_type == "group" else "私聊"
            lines.append(
                f"- target_key={target.target_key} | {target.platform}{chat_label} | "
                f"{target.display_name}"
            )
        lines.extend([
            "",
            "这些是你最近可以触达的会话。如果心里有想对谁说的话，"
            "可以用 `nucleus_schedule_autonomy_intent`（kind=speak，填 target_key）"
            "登记一个意向，到点交给表达层重新判断；"
            "没有想说的话也很好——看见他们在那里，本身就够了。",
        ])
        return "\n".join(lines)


DEFAULT_HEARTBEAT_SECTIONS: list[HeartbeatSectionProvider] = [
    InnerStateSection(),
    ThoughtStreamsSection(),
    CuriositySection(),
    ImpulseSection(),
    SendTargetsSection(),
]
