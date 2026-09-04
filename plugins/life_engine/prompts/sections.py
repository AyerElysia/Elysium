"""心跳 prompt 注入段落的统一协议（SectionProvider）。

可言说法则：**任何子系统向心跳注入文本，都实现同一个 SectionProvider 接口；
装配方只做循环。** 新增子系统注入 = 新增一个 provider，不再修改装配代码。

每个 provider 自己持有"是否注入"的配置判断（enabled）与渲染逻辑（render），
一个 provider 渲染失败不影响其他段落。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import json
import re

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


class OpportunitySection(HeartbeatSectionProvider):
    """One heartbeat opportunity page: continuity clues plus infrastructure invitations."""

    section_id = "opportunity_page"

    async def render(self, ctx: SectionContext) -> str | None:
        bus = getattr(ctx.service, "_opportunity_bus", None)
        if bus is None:
            return None
        page = await bus.collect_and_render(config=ctx.config)
        if page is None:
            return None
        return page.text


_DIARY_DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def build_handwritten_diary_inventory(
    diaries_root: Path,
    *,
    today: str,
    limit_dates: int = 16,
) -> str | None:
    """List top-level dated handwritten diaries, excluding witness projections.

    This is a workspace census, not an importance ranking or a writing prompt.
    """
    if not diaries_root.is_dir():
        return None

    dated: dict[str, list[Path]] = {}
    for entry in diaries_root.iterdir():
        if not entry.is_file() or entry.suffix.lower() != ".md":
            continue
        match = _DIARY_DATE_PREFIX.match(entry.name)
        if match is None:
            continue
        dated.setdefault(match.group(1), []).append(entry)
    if not dated:
        return None

    total = sum(len(paths) for paths in dated.values())
    chronological = sorted(dated)
    recent_dates = list(reversed(chronological))[: max(1, int(limit_dates))]
    lines = [
        "### 手写日记目录（工作区事实，非见证回望）",
        "",
        f"`diaries/` 根目录共有 {total} 篇带日期的手写 Markdown，不含 `diaries/witness/` 见证投影。",
        f"最早一篇日期 {chronological[0]}。下面是最近 {len(recent_dates)} 个有日记的日期，从新到旧。",
        "",
        "这是文件清单，不是任务，不是重要性排序，也不催你写。若要打开某篇，用 `nucleus_read_file`。",
        "",
    ]
    for day in recent_dates:
        files = sorted(dated[day], key=lambda path: path.name)
        primary = diaries_root / f"{day}.md"
        if not primary.is_file():
            primary = files[0]
        modified = datetime.fromtimestamp(
            primary.stat().st_mtime, tz=timezone.utc
        ).astimezone().isoformat(timespec="minutes")
        extra = f" 等{len(files)}篇" if len(files) > 1 else ""
        marker = "（今天）" if day == today else ""
        lines.append(
            f"- {day}{marker}  {len(files)}篇  `{primary.name}`{extra}  {modified}"
        )
    return "\n".join(lines)


class RecentHandwrittenDiariesSection(HeartbeatSectionProvider):
    """Deterministic census of recent handwritten diary files."""

    section_id = "recent_handwritten_diaries"

    async def render(self, ctx: SectionContext) -> str | None:
        workspace_fn = getattr(ctx.service, "_workspace_dir", None)
        if workspace_fn is None:
            return None
        try:
            diaries_root = workspace_fn() / "diaries"
        except Exception:  # noqa: BLE001
            return None
        return build_handwritten_diary_inventory(
            diaries_root, today=str(ctx.today_str or "")
        )


_FILE_CARE_STATE = "runtime/file_care_state.json"
_FILE_CARE_COOLDOWN_HOURS = 168.0
_FILE_CARE_MIN_TOP_LEVEL = 60
_FILE_CARE_MIN_DAY_FRAGMENTS = 6


def inspect_handwritten_diary_clutter(
    diaries_root: Path,
    *,
    min_top_level: int = _FILE_CARE_MIN_TOP_LEVEL,
    min_day_fragments: int = _FILE_CARE_MIN_DAY_FRAGMENTS,
) -> dict[str, Any] | None:
    """Return a census when top-level handwritten diaries look crowded.

    Does not recommend a filing scheme. Witness projections are ignored.
    """
    if not diaries_root.is_dir():
        return None
    dated: dict[str, list[str]] = {}
    for entry in diaries_root.iterdir():
        if not entry.is_file() or entry.suffix.lower() != ".md":
            continue
        match = _DIARY_DATE_PREFIX.match(entry.name)
        if match is None:
            continue
        dated.setdefault(match.group(1), []).append(entry.name)
    if not dated:
        return None
    total = sum(len(names) for names in dated.values())
    busiest_day, busiest_names = max(
        dated.items(), key=lambda item: (len(item[1]), item[0])
    )
    if total < min_top_level and len(busiest_names) < min_day_fragments:
        return None
    return {
        "total": total,
        "days": len(dated),
        "earliest": min(dated),
        "latest": max(dated),
        "busiest_day": busiest_day,
        "busiest_count": len(busiest_names),
    }


def build_file_care_invitation(census: dict[str, Any]) -> str:
    """Invitation copy: facts only, no filing command."""
    return "\n".join(
        [
            "### 文件收拾机会（不是任务）",
            "",
            f"`diaries/` 根目录现在摊着 {census['total']} 篇带日期的手写日记，覆盖 {census['days']} 天"
            f"（{census['earliest']} → {census['latest']}）。"
            f"最挤的一天是 {census['busiest_day']}，当天有 {census['busiest_count']} 个文件。",
            "",
            "`diaries/witness/` 是记忆见证的回望投影，不是你的手写本，不要去整理那棵树。",
            "",
            "这不是命令，也不是重要性评分。旧文件不能搬、不能删——记忆账本把路径当成历史身份。"
            "若你觉得乱，可以给以后的新文字建子目录（`nucleus_mkdir`），或写一篇目录方便自己找；"
            "继续摊在根目录也同样完整。查找时可用 `max_depth=1` 或 `exclude_glob=diaries/witness`。",
            "",
            "现在不想收拾，跳过就好。",
        ]
    )


def _file_care_state_path(workspace: Path) -> Path:
    return workspace / _FILE_CARE_STATE


def _read_file_care_invited_at(workspace: Path) -> datetime | None:
    path = _file_care_state_path(workspace)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = str(payload.get("last_invited_at") or "").strip()
        if not raw:
            return None
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_file_care_invited_at(workspace: Path, when: datetime) -> None:
    path = _file_care_state_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"last_invited_at": when.isoformat()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


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


class TodoBoardSection(HeartbeatSectionProvider):
    """Compact durable TODO board. State, not an invitation or ranking."""

    section_id = "todo_board"

    async def render(self, ctx: SectionContext) -> str | None:
        from ..tools.todo_tools import TodoStorage, format_todo_board

        workspace_fn = getattr(ctx.service, "_workspace_dir", None)
        if not callable(workspace_fn):
            return None
        try:
            storage = TodoStorage(workspace_fn())
            todos = storage.load()
        except Exception:  # noqa: BLE001
            return None
        return format_todo_board(todos)


class HeartbeatCapabilityCatalogSection(HeartbeatSectionProvider):
    """Names capabilities without repeating ROLE.TOOL schemas."""

    section_id = "capability_catalog"
    _MAX_BYTES = 1024

    async def render(self, ctx: SectionContext) -> str | None:
        text = "\n".join(
            [
                "### 能力目录（不是任务）",
                "",
                "本窗口可调用的工具以 ROLE.TOOL 为准，这里不重复参数表。",
                "机会页邀请栏只给到期事实。动手说明书要么是 skill，要么是已常驻工具。",
                "学习：`nucleus_learn action=help` 读 `skills/learning/SKILL.md`。",
                "MEMORY：文件工具可改；结构化整理可用 continuity_review。长河：`nucleus_write_narrative`。",
                "文件收拾：`nucleus_read_file` / `nucleus_edit_file` / `nucleus_apply_patch` / `nucleus_mkdir`。",
                "日记旧路径勿搬删；SOUL/USER/MEMORY/EXISTENCE 与日记同类。",
                "忽略、不调用、安静结束都完整，不等于拒绝；到期不会自动打开 skill 正文。",
                "未注入本拍但仍存在的能力在聊天或其他意识窗口：",
                "`nucleus_bash`、`nucleus_view_screen`、`nucleus_run_agent`、联网、",
                "`platform_action`、`conversation_evidence`、`nucleus_trace`、",
                "`nucleus_relations`、`nucleus_memory_stats`、表情收藏。",
                "没出现在本轮 schema ≠ 主体不想用。",
            ]
        )
        encoded = text.encode("utf-8")
        if len(encoded) <= self._MAX_BYTES:
            return text
        clipped = encoded[: self._MAX_BYTES]
        while clipped:
            try:
                return clipped.decode("utf-8")
            except UnicodeDecodeError:
                clipped = clipped[:-1]
        return None


DEFAULT_HEARTBEAT_SECTIONS: list[HeartbeatSectionProvider] = [
    RecentHandwrittenDiariesSection(),
    TodoBoardSection(),
    OpportunitySection(),
    HeartbeatCapabilityCatalogSection(),
    SelfKnowledgeSection(),
    SkillCatalogSection(),
    LearningProgressSection(),
]
