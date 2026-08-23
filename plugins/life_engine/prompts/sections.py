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
        if getattr(ctx.service, "_proactive_authority", None) is not None:
            return True
        curiosity_cfg = getattr(ctx.config, "curiosity", None)
        curiosity_enabled = curiosity_cfg is None or (
            bool(getattr(curiosity_cfg, "enabled", True))
            and bool(getattr(curiosity_cfg, "inject_to_heartbeat", True))
        )
        return curiosity_enabled

    async def render(self, ctx: SectionContext) -> str | None:
        service = ctx.service
        blocks: list[str] = []

        attention_available = getattr(service, "_proactive_authority", None) is not None
        if attention_available:
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

        from ..narrative.store import _parse_iso

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


DEFAULT_HEARTBEAT_SECTIONS: list[HeartbeatSectionProvider] = [
    AttentionOpportunitySection(),
    RiverReflectionSection(),
    SelfKnowledgeSection(),
    SkillCatalogSection(),
    LearningProgressSection(),
    SubjectReviewOpportunitySection(),
]
