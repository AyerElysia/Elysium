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


class ThoughtStreamsSection(HeartbeatSectionProvider):
    """当前活跃思考流（heartbeat 内不分组、不做 delta）。"""

    section_id = "thought_streams"

    def enabled(self, ctx: SectionContext) -> bool:
        if getattr(ctx.service, "_thought_manager", None) is None:
            return False
        streams_cfg = getattr(ctx.config, "streams", None)
        return (
            streams_cfg is None
            or bool(getattr(streams_cfg, "inject_to_heartbeat", True))
        )

    async def render(self, ctx: SectionContext) -> str | None:
        streams_cfg = getattr(ctx.config, "streams", None)
        focus_window = (
            int(getattr(streams_cfg, "focus_window_minutes", 30) or 30)
            if streams_cfg
            else 30
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
    """Deprecated section exposing only an external epistemic candidate."""

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
        return body


class AttentionOpportunitySection(HeartbeatSectionProvider):
    """Present open thought and curiosity evidence once, without choosing an action."""

    section_id = "attention_opportunity"

    def enabled(self, ctx: SectionContext) -> bool:
        if getattr(ctx.service, "_attention_thread_service", None) is not None:
            return True
        streams_cfg = getattr(ctx.config, "streams", None)
        curiosity_cfg = getattr(ctx.config, "curiosity", None)
        streams_enabled = streams_cfg is None or bool(
            getattr(streams_cfg, "inject_to_heartbeat", True)
        )
        curiosity_enabled = curiosity_cfg is None or (
            bool(getattr(curiosity_cfg, "enabled", True))
            and bool(getattr(curiosity_cfg, "inject_to_heartbeat", True))
        )
        return streams_enabled or curiosity_enabled

    async def render(self, ctx: SectionContext) -> str | None:
        service = ctx.service
        blocks: list[str] = []

        streams_cfg = getattr(ctx.config, "streams", None)
        streams_enabled = streams_cfg is None or bool(
            getattr(streams_cfg, "inject_to_heartbeat", True)
        )
        manager = getattr(service, "_thought_manager", None)
        attention = getattr(service, "_attention_thread_service", None)
        if attention is not None:
            from ..attention_threads import AttentionThreadPageQuery

            try:
                page = await service.page_attention_threads(
                    AttentionThreadPageQuery(
                        statuses=("open", "paused"),
                        limit=16,
                        max_bytes=16 * 1024,
                        projection_kind="heartbeat_attention_opportunity",
                        focus_instance_id="chat_global",
                    )
                )
            except Exception as exc:  # noqa: BLE001 - health reports exact failure
                logger.debug(
                    "canonical attention projection unavailable: "
                    f"{type(exc).__name__}"
                )
            else:
                if page.items:
                    blocks.append(f"#### 主体持续关注线索\n{page.content}")
        elif streams_enabled and manager is not None:
            focus_window = (
                int(getattr(streams_cfg, "focus_window_minutes", 30) or 30)
                if streams_cfg
                else 30
            )
            body = manager.format_for_prompt(
                max_items=3,
                focus_window_minutes=focus_window,
                grouped=False,
                mark_delta=False,
            )
            if body:
                blocks.append(f"#### 现有思考线索\n{body}")

        curiosity_cfg = getattr(ctx.config, "curiosity", None)
        curiosity_enabled = curiosity_cfg is None or (
            bool(getattr(curiosity_cfg, "enabled", True))
            and bool(getattr(curiosity_cfg, "inject_to_heartbeat", True))
        )
        if curiosity_enabled:
            try:
                body = await service._get_curiosity_engine().format_for_prompt(
                    max_chars=1200
                )
            except Exception:  # noqa: BLE001
                body = ""
            if body:
                blocks.append(body)

        if not blocks:
            return None

        lines = [
            "### 注意机会（attention_opportunity）",
            "",
            "以下只是当前可见的未闭合线索投影，不是任务、命令或行动请求。",
            "它不替你决定创建、推进、记录、搁置或关闭任何线索；是否回应、如何回应都由你自己决定。",
            "",
            "\n\n".join(blocks),
            "",
            "保持原样或安静结束本轮同样是完整决定。",
        ]
        return "\n".join(lines)


class ImpulseSection(HeartbeatSectionProvider):
    """冲动引擎的建议（纯建议，可遵循可忽略）。"""

    section_id = "impulses"

    def enabled(self, ctx: SectionContext) -> bool:
        if getattr(ctx.service, "_impulse_engine", None) is None:
            return False
        drives_cfg = getattr(ctx.config, "drives", None)
        return (
            drives_cfg is None
            or bool(getattr(drives_cfg, "inject_to_heartbeat", True))
        )

    async def render(self, ctx: SectionContext) -> str | None:
        service = ctx.service
        
        # 判定紧急 todo
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
        
        # 判定好奇刺点
        has_curiosity_signal = False
        try:
            signal = await service._get_curiosity_engine().load_signal()
            has_curiosity_signal = bool(signal and signal.text)
        except Exception:  # noqa: BLE001
            has_curiosity_signal = False
        
        # 判定学习进展
        has_learning_progress = False
        try:
            scheduler = getattr(service, "_learning_scheduler", None)
            if scheduler:
                progress = scheduler.get_progress_for_prompt()
                has_learning_progress = bool(progress)
        except Exception:  # noqa: BLE001
            has_learning_progress = False
        
        # 判定待沉淀河流记忆
        has_pending_river_moments = False
        try:
            from ..narrative.store import NarrativeStore
            from ..trace.store import LifeTraceStore
            narrative_cfg = getattr(ctx.config, "narrative", None)
            if narrative_cfg and getattr(narrative_cfg, "enabled", True):
                store = service.narrative_store()
                state = await store.load_state()
                trace_store = service.life_trace_store()
                pending = store.pending_moments(
                    await trace_store.recent(limit=500),
                    state,
                )
                min_moments = int(getattr(narrative_cfg, "min_moments", 5))
                has_pending_river_moments = len(pending) >= min_moments
        except Exception:  # noqa: BLE001
            if getattr(service, "_selectable_storage_enabled", False):
                raise
            has_pending_river_moments = False
        
        # 判定自主意向（需要新增工具支持，暂时置 False）
        has_autonomy_intents = False
        
        context = {
            "silence_minutes": ctx.silence_minutes or 0,
            "idle_heartbeats": ctx.idle_heartbeats,
            "has_active_thoughts": bool(
                getattr(service, "_thought_manager", None)
                and service._thought_manager.list_active()
            ),
            "has_urgent_todos": has_urgent_todos,
            "has_curiosity_signal": has_curiosity_signal,
            "has_learning_progress": has_learning_progress,
            "has_pending_river_moments": has_pending_river_moments,
            "has_autonomy_intents": has_autonomy_intents,
        }
        suggestions = service._impulse_engine.evaluate({}, context)
        return service._impulse_engine.format_for_prompt(
            suggestions, {}, max_items=3
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


class RiverReflectionSection(HeartbeatSectionProvider):
    """回望长河：到期时把未沉淀的转折点摆给她，由她决定是否讲述。

    沉淀必须经过她的语言——本段落只呈现素材与邀请，绝不替她总结；
    低频（min_interval_hours）+ 邀请冷却（invite_cooldown_hours），
    回望是想起，不是作业。
    """

    section_id = "river_reflection"

    def enabled(self, ctx: SectionContext) -> bool:
        narrative_cfg = getattr(ctx.config, "narrative", None)
        if (
            narrative_cfg is None
            or getattr(ctx.service, "_workspace_dir", None) is None
        ):
            return False
        return bool(getattr(narrative_cfg, "enabled", True)) and bool(
            getattr(narrative_cfg, "inject_to_heartbeat", True)
        )

    async def render(self, ctx: SectionContext) -> str | None:
        from datetime import datetime, timezone

        from ..narrative.store import NarrativeStore, _parse_iso
        from ..trace.store import LifeTraceStore

        cfg = ctx.config.narrative
        store = ctx.service.narrative_store()
        state = await store.load_state()
        now = datetime.now(timezone.utc).astimezone()

        last_consolidated = _parse_iso(state.get("last_consolidated_at", ""))
        if last_consolidated is not None:
            elapsed_hours = (now - last_consolidated).total_seconds() / 3600.0
            if elapsed_hours < float(cfg.min_interval_hours):
                return None

        last_invited = _parse_iso(state.get("last_invited_at", ""))
        if last_invited is not None:
            since_invite = (now - last_invited).total_seconds() / 3600.0
            if since_invite < float(cfg.invite_cooldown_hours):
                return None

        pending = store.pending_moments(
            await ctx.service.life_trace_store().recent(limit=500),
            state,
        )
        if len(pending) < int(cfg.min_moments):
            return None

        await store.mark_invited(now=now)

        shown = pending[-int(cfg.max_moments_shown):]
        lines = [
            "### 回望长河",
            "",
            f"自上次沉淀以来，你的长河里多了 {len(pending)} 条留痕：",
            "",
        ]
        for record in shown:
            label = record.summary or record.path or record.operation
            lines.append(f"- {record.timestamp[:16]} [{record.kind}] {label}")
        if len(pending) > len(shown):
            lines.append(f"- ……以及更早的 {len(pending) - len(shown)} 条")
        last_entry = await store.last_entry()
        if last_entry is not None and last_entry.text:
            snippet = last_entry.text[:80]
            lines.extend(["", f"上次你写道：「{snippet}」"])
        lines.extend([
            "",
            "如果你愿意回望这段日子，可以用 `nucleus_write_narrative` "
            "写下它对你意味着什么——"
            "用你自己的话，长短不限。"
            "如果你觉得没什么值得说的，传 nothing_to_say=true "
            "也同样是一次完整的回望。"
            "现在不想回望，跳过也很好。",
        ])
        return "\n".join(lines)


class SelfKnowledgeSection(HeartbeatSectionProvider):
    """可修订的学习观察投影；绝不作为主体身份权威。"""

    section_id = "self_knowledge"

    def enabled(self, ctx: SectionContext) -> bool:
        learning_cfg = getattr(ctx.config, "learning", None)
        if learning_cfg is None:
            return True
        return bool(getattr(learning_cfg, "enabled", True)) and bool(
            getattr(learning_cfg, "inject_to_heartbeat", True)
        )

    async def render(self, ctx: SectionContext) -> str | None:
        service = ctx.service
        scheduler = getattr(service, "_learning_scheduler", None)
        if scheduler is None:
            return None
        knowledge = scheduler.get_knowledge_for_prompt(
            max_chars=int(
                getattr(ctx.config.learning, "knowledge_max_chars", 2000)
                or 2000
            )
        )
        if not knowledge:
            return None
        return f"### 学习观察账本（可质疑，非主体权威）\n{knowledge}"


class LearningProgressSection(HeartbeatSectionProvider):
    """近期学习账本状态；状态不是主体真值或价值判断。"""

    section_id = "learning_progress"

    def enabled(self, ctx: SectionContext) -> bool:
        learning_cfg = getattr(ctx.config, "learning", None)
        if learning_cfg is None:
            return True
        return bool(getattr(learning_cfg, "enabled", True)) and bool(
            getattr(learning_cfg, "inject_to_heartbeat", True)
        )

    async def render(self, ctx: SectionContext) -> str | None:
        service = ctx.service
        scheduler = getattr(service, "_learning_scheduler", None)
        if scheduler is None:
            return None
        progress = scheduler.get_progress_for_prompt()
        return progress or None


class SubjectReviewOpportunitySection(HeartbeatSectionProvider):
    """Low-frequency invitation to revisit authority documents, never a task."""

    section_id = "subject_review_opportunity"

    def enabled(self, ctx: SectionContext) -> bool:
        learning_cfg = getattr(ctx.config, "learning", None)
        return bool(
            learning_cfg is not None
            and getattr(learning_cfg, "enabled", True)
            and getattr(learning_cfg, "subject_review_enabled", True)
        )

    async def render(self, ctx: SectionContext) -> str | None:
        scheduler = getattr(ctx.service, "_learning_scheduler", None)
        if scheduler is None:
            return None
        prompt = await scheduler.get_subject_review_prompt()
        return prompt or None


class SkillCatalogSection(HeartbeatSectionProvider):
    """程序性学习账本；由当前意识决定是否采用其中的做法。

    只呈现目录（description + 成熟度），不呈现完整 instructions。
    她决定什么时候细看。用不用、什么时候用，完全由她在推理中自主判断。
    """

    section_id = "skill_catalog"

    def enabled(self, ctx: SectionContext) -> bool:
        learning_cfg = getattr(ctx.config, "learning", None)
        if learning_cfg is None:
            return True
        return bool(getattr(learning_cfg, "enabled", True)) and bool(
            getattr(learning_cfg, "inject_to_heartbeat", True)
        )

    async def render(self, ctx: SectionContext) -> str | None:
        service = ctx.service
        scheduler = getattr(service, "_learning_scheduler", None)
        if scheduler is None:
            return None
        max_chars = int(
            getattr(
                getattr(ctx.config, "learning", None),
                "skill_catalog_max_chars",
                600,
            )
            or 600
        )
        catalog = scheduler.get_skill_catalog_for_prompt(max_chars=max_chars)
        if not catalog:
            return None
        return f"### 程序性学习账本（可质疑，非主体权威）\n{catalog}"


class LeisureOpportunitySection(HeartbeatSectionProvider):
    """休闲机会快照：将现存可审计状态组织为非强制候选集合。
    
    设计原则：
    - 以邀请而非任务形式呈现机会
    - 保持主体性：有候选时仍允许选择休息或安静结束
    - 基于现存可审计状态，不依赖已删除的 neuromod
    """

    section_id = "leisure_opportunities"

    def enabled(self, ctx: SectionContext) -> bool:
        # 复用原 drives 配置键以保持兼容性
        drives_cfg = getattr(ctx.config, "drives", None)
        return (
            drives_cfg is None
            or bool(getattr(drives_cfg, "inject_to_heartbeat", True))
        )

    async def render(self, ctx: SectionContext) -> str | None:
        service = ctx.service
        opportunities: list[str] = []
        
        # 1. 活跃思考流
        if (
            getattr(service, "_thought_manager", None)
            and service._thought_manager.list_active()
        ):
            opportunities.append(
                "你有未完成的思考流，也许可以继续深入、联想或沉淀"
            )
        
        # 2. 好奇刺点
        try:
            signal = await service._get_curiosity_engine().load_signal()
            if signal and signal.text:
                opportunities.append("好奇层留下了刺点；如果你在意，可以靠近它、开思考流承接")
        except Exception:  # noqa: BLE001
            pass
        
        # 3. 学习进展
        try:
            scheduler = getattr(service, "_learning_scheduler", None)
            if scheduler and scheduler.get_progress_for_prompt():
                opportunities.append("学习系统有新进展；可以看看新验证的领悟或技能目录")
        except Exception:  # noqa: BLE001
            pass
        
        # 4. 待沉淀河流记忆
        try:
            from ..narrative.store import NarrativeStore
            from ..trace.store import LifeTraceStore
            narrative_cfg = getattr(ctx.config, "narrative", None)
            if narrative_cfg and getattr(narrative_cfg, "enabled", True):
                store = service.narrative_store()
                state = await store.load_state()
                trace_store = service.life_trace_store()
                pending = store.pending_moments(
                    await trace_store.recent(limit=500),
                    state,
                )
                min_moments = int(getattr(narrative_cfg, "min_moments", 5))
                if len(pending) >= min_moments:
                    opportunities.append("长河里积累了一些留痕；如果愿意，可以回望并写下它对你意味着什么")
        except Exception:  # noqa: BLE001
            if getattr(service, "_selectable_storage_enabled", False):
                raise
        
        # 5. 紧急 todo（作为提醒而非命令）
        try:
            from ..tools.todo_tools import TodoStorage
            active_todos = [
                todo
                for todo in TodoStorage(service._workspace_dir()).load()
                if todo.status not in {"completed", "cancelled", "archived"}
            ]
            if any(
                todo.priority == "urgent"
                or todo.is_overdue()
                or todo.needs_review()
                for todo in active_todos
            ):
                opportunities.append(
                    "有紧急或逾期的 TODO；这是承诺提醒，可以观察、整理或释放"
                )
        except Exception:  # noqa: BLE001
            pass
        
        if not opportunities:
            return None
        
        lines = ["### 当前机会", ""]
        lines.append("此刻你可能想：")
        lines.append("")
        for opp in opportunities[:4]:  # 最多展示 4 条
            lines.append(f"- {opp}")
        lines.append("")
        lines.append("这些只是机会；你也可以观察、沉淀，或者安静结束本轮。")
        lines.append("如果精力需要恢复，主动休息也很好。")
        lines.append("")
        
        return "\n".join(lines)


DEFAULT_HEARTBEAT_SECTIONS: list[HeartbeatSectionProvider] = [
    AttentionOpportunitySection(),
    SendTargetsSection(),
    RiverReflectionSection(),
    SelfKnowledgeSection(),
    SkillCatalogSection(),
    LearningProgressSection(),
    SubjectReviewOpportunitySection(),
]
