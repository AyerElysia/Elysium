"""MEMORY.md 解析、注入与维护辅助逻辑。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
import re

from ..constants import (
    MEMORY_CHATTER_MAX_ACTIVE_ITEMS,
    MEMORY_CHATTER_MAX_DURABLE_ITEMS,
    MEMORY_MAINTENANCE_INTERVAL_HOURS,
    MEMORY_MAX_DURABLE_ITEMS,
    MEMORY_MAX_FADING_ITEMS,
    MEMORY_PROMPT_SOFT_LIMIT_BYTES,
    MEMORY_WRITE_WARNING_THRESHOLD_BYTES,
)

_SECTION_PATTERN = re.compile(r"^###\s*(.+?)\s*$")
_WHITESPACE_PATTERN = re.compile(r"\s+")

_HEARTBEAT_MEMORY_BOUNDARY_LINES = [
    "### 心跳态边界",
    "",
    "- 这些记忆是历史事实和关系线索，不是当前心跳的行动指令。",
    "- 历史中出现过的画画、bash、创建文件或主动表达，不自动授权潜意识在本轮复现。",
    "- 涉及用户任务、项目操作、生成图片或对外表达时，只能形成信息差候选，交给表达层判断。",
]


@dataclass(slots=True)
class MemorySection:
    """MEMORY.md 中的一个结构化区块。"""

    key: str
    heading: str
    items: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MemoryPromptData:
    """供 prompt / 工具使用的 MEMORY 分析结果。"""

    raw_text: str
    sections: dict[str, MemorySection]
    size_bytes: int
    maintenance_reasons: list[str]
    prompt_warning_lines: list[str]

    @property
    def durable_items(self) -> list[str]:
        return list(self.sections["durable"].items)

    @property
    def active_items(self) -> list[str]:
        return list(self.sections["active"].items)

    @property
    def fading_items(self) -> list[str]:
        return list(self.sections["fading"].items)

    @property
    def needs_maintenance(self) -> bool:
        return bool(self.maintenance_reasons)


def analyze_memory_text(text: str) -> MemoryPromptData:
    """分析 MEMORY.md 文本，提取结构化条目和维护信号。"""
    raw_text = str(text or "").strip()
    sections = _parse_sections(raw_text)
    size_bytes = len(raw_text.encode("utf-8"))
    maintenance_reasons = _build_maintenance_reasons(sections, size_bytes)
    prompt_warning_lines = _build_prompt_warning_lines(
        size_bytes=size_bytes,
        durable_count=len(sections["durable"].items),
        active_count=len(sections["active"].items),
        fading_count=len(sections["fading"].items),
        reasons=maintenance_reasons,
    )
    return MemoryPromptData(
        raw_text=raw_text,
        sections=sections,
        size_bytes=size_bytes,
        maintenance_reasons=maintenance_reasons,
        prompt_warning_lines=prompt_warning_lines,
    )


def render_memory_prompt(
    data: MemoryPromptData,
    *,
    mode: str,
) -> str:
    """渲染供 system prompt 注入的 MEMORY 文本。"""
    durable_items = data.durable_items
    active_items = data.active_items
    notes: list[str] = []

    if mode == "chat":
        durable_total = len(durable_items)
        active_total = len(active_items)
        durable_items = durable_items[:MEMORY_CHATTER_MAX_DURABLE_ITEMS]
        active_items = active_items[:MEMORY_CHATTER_MAX_ACTIVE_ITEMS]
        if durable_total > len(durable_items):
            notes.append(
                f"- 聊天态仅注入前 {len(durable_items)} 条 Durable；其余请按需检索。"
            )
        if active_total > len(active_items):
            notes.append(
                f"- 聊天态仅注入前 {len(active_items)} 条 Active；其余请按需检索。"
            )

    lines: list[str] = ["# 值得记住的事（MEMORY 摘要）", ""]
    if mode == "heartbeat":
        lines.extend(_HEARTBEAT_MEMORY_BOUNDARY_LINES)
        lines.append("")
    if data.prompt_warning_lines:
        lines.extend(data.prompt_warning_lines)
        lines.append("")

    lines.extend(_render_section("### Durable（持久）", durable_items))
    if active_items:
        lines.append("")
        lines.extend(_render_section("### Active（活跃）", active_items))
    if notes:
        lines.append("")
        lines.append("### 注入说明")
        lines.append("")
        lines.extend(notes)

    return "\n".join(lines).strip()


def build_memory_write_warning(path: str, content: str) -> str | None:
    """检查写入内容是否可能意外覆盖大量记忆。"""
    size = len(content.encode("utf-8"))
    if size >= MEMORY_WRITE_WARNING_THRESHOLD_BYTES:
        return (
            f"⚠️ 此文件内容较大（{size // 1024}KB），"
            "请确认不是意外覆盖整个记忆文档"
        )
    return None


def format_memory_bundles_for_prompt(
    bundles: list,
    *,
    max_bundles: int = 3,
    include_history: bool = True,
    include_corrections: bool = True,
) -> str:
    """完美架构：将 MemoryBundle 列表格式化为结构化的 prompt 文本。

    Args:
        bundles: MemoryBundle 列表
        max_bundles: 最多包含多少个记忆包
        include_history: 是否包含历史演化轨迹
        include_corrections: 是否包含修正记录

    Returns:
        格式化的 prompt 文本，包含：
        - 当前理解（主要依据）
        - 历史演化（如果有）
        - 已知修正（如果有）
        - 不确定性说明（如果有）
    """
    if not bundles:
        return ""

    lines = ["<memory_context>"]

    for idx, bundle in enumerate(bundles[:max_bundles], 1):
        if idx > 1:
            lines.append("")
            lines.append("---")
            lines.append("")

        # 当前理解
        lines.append(f"## 记忆 {idx}: {bundle.query}")
        lines.append("")
        lines.append("### 当前理解")
        lines.append(f"- 主要文件: {bundle.primary_path}")

        if bundle.evidence:
            lines.append("- 相关证据:")
            for evidence in bundle.evidence[:5]:  # 最多5个证据
                relevance_str = f"相关度 {evidence.relevance:.2f}"
                source_str = ""
                if evidence.source == "lineage":
                    source_str = f" (演化关系: {evidence.relation})"
                elif evidence.source == "associated":
                    source_str = " (联想)"

                lines.append(f"  - {evidence.file_path} ({relevance_str}{source_str})")

        # 历史演化
        if include_history and bundle.history_trace:
            lines.append("")
            lines.append("### 历史演化")

            # 分组：earlier 和 later
            earlier = [t for t in bundle.history_trace if t.direction == "earlier"]
            later = [t for t in bundle.history_trace if t.direction == "later"]

            if earlier:
                lines.append("- 早期版本:")
                for trace in earlier:
                    reason_str = f" - {trace.reason}" if trace.reason else ""
                    lines.append(f"  - {trace.file_path} [{trace.relation}]{reason_str}")

            if later:
                lines.append("- 后续演化:")
                for trace in later:
                    reason_str = f" - {trace.reason}" if trace.reason else ""
                    lines.append(f"  - {trace.file_path} [{trace.relation}]{reason_str}")

        # 已知修正
        if include_corrections and bundle.corrections:
            lines.append("")
            lines.append("### 已知修正")
            for correction in bundle.corrections[:3]:  # 最多3条修正
                source_label = {
                    "user": "用户纠正",
                    "reflection": "反思修正",
                    "system": "系统修正",
                }.get(correction.source, correction.source)

                # 格式化时间
                from datetime import datetime
                dt = datetime.fromtimestamp(correction.created_at)
                time_str = dt.strftime("%Y-%m-%d")

                lines.append(f"- [{source_label}, {time_str}] {correction.message}")

        # 不确定性
        if bundle.uncertainty and bundle.uncertainty.strip():
            lines.append("")
            lines.append("### 不确定性")
            lines.append(f"- {bundle.uncertainty}")

    lines.append("")
    lines.append("</memory_context>")

    return "\n".join(lines)


def build_memory_maintenance_prompt(data: MemoryPromptData) -> str:
    """生成心跳期的 MEMORY 维护提示。"""
    if not data.needs_maintenance:
        return ""

    lines = [
        "### MEMORY 维护任务（本轮优先）",
        "",
        (
            f"- 当前 MEMORY.md：{_format_kb(data.size_bytes)}；"
            f"Durable {len(data.durable_items)} 条，"
            f"Active {len(data.active_items)} 条，"
            f"Fading {len(data.fading_items)} 条。"
        ),
    ]
    for reason in data.maintenance_reasons[:3]:
        lines.append(f"- {reason}")
    lines.extend(
        [
            "- 详细写入规则请先看 `MEMORY_GUIDE.md`。",
            "- 如果有明显长叙事、时间线或情感展开，可迁到 `notes/`、`thoughts/` 或 `diaries/`；MEMORY.md 保留决策级摘要即可。",
            "- 如果决定整理，本轮做一个具体动作就够：迁移一段叙事、合并一条 Durable、或清理一条 Fading。",
        ]
    )
    return "\n".join(lines)


def should_emit_memory_maintenance_prompt(
    data: MemoryPromptData,
    last_prompt_at: str | None,
    *,
    now: datetime | None = None,
) -> bool:
    """判断当前是否应该再次向心跳注入 MEMORY 维护任务。"""
    if not data.needs_maintenance:
        return False

    if not last_prompt_at:
        return True

    try:
        last_dt = datetime.fromisoformat(last_prompt_at)
    except ValueError:
        return True

    current = now or datetime.now().astimezone()
    return current - last_dt >= timedelta(hours=MEMORY_MAINTENANCE_INTERVAL_HOURS)


def _parse_sections(text: str) -> dict[str, MemorySection]:
    sections = {
        "durable": MemorySection("durable", "### Durable（持久）"),
        "active": MemorySection("active", "### Active（活跃）"),
        "fading": MemorySection("fading", "### Fading（待审视）"),
    }
    current_key: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        match = _SECTION_PATTERN.match(line.strip())
        if match:
            current_key = _normalize_section_key(match.group(1))
            continue
        if current_key is None:
            continue

        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            sections[current_key].items.append(_normalize_item(stripped[2:]))
            continue
        if sections[current_key].items and not stripped.startswith("#"):
            previous = sections[current_key].items[-1]
            sections[current_key].items[-1] = _normalize_item(f"{previous} {stripped}")

    return sections


def _normalize_section_key(heading: str) -> str | None:
    lowered = heading.lower()
    if "durable" in lowered or "持久" in heading:
        return "durable"
    if "active" in lowered or "活跃" in heading:
        return "active"
    if "fading" in lowered or "待审视" in heading:
        return "fading"
    return None


def _normalize_item(item: str) -> str:
    return _WHITESPACE_PATTERN.sub(" ", item).strip()


def _render_section(title: str, items: list[str]) -> list[str]:
    lines = [title, ""]
    if not items:
        lines.append("- （当前为空）")
        return lines
    lines.extend(f"- {item}" for item in items)
    return lines


def _build_maintenance_reasons(
    sections: dict[str, MemorySection],
    size_bytes: int,
) -> list[str]:
    reasons: list[str] = []
    durable_count = len(sections["durable"].items)
    fading_count = len(sections["fading"].items)

    if size_bytes > MEMORY_PROMPT_SOFT_LIMIT_BYTES:
        reasons.append(
            f"文件大小 {_format_kb(size_bytes)}，超过建议上限 {_format_kb(MEMORY_PROMPT_SOFT_LIMIT_BYTES)}"
        )
    if durable_count > MEMORY_MAX_DURABLE_ITEMS:
        reasons.append(
            f"Durable 条目 {durable_count} 条，超过建议上限 {MEMORY_MAX_DURABLE_ITEMS} 条"
        )
    if fading_count > MEMORY_MAX_FADING_ITEMS:
        reasons.append(
            f"Fading 条目 {fading_count} 条，说明有待迁移或删除的存量"
        )
    return reasons


def _build_prompt_warning_lines(
    *,
    size_bytes: int,
    durable_count: int,
    active_count: int,
    fading_count: int,
    reasons: list[str],
) -> list[str]:
    if not reasons:
        return []

    lines = [
        "### MEMORY 状态提醒",
        "",
        (
            f"- 当前文件：{_format_kb(size_bytes)}；"
            f"Durable {durable_count} 条，Active {active_count} 条，Fading {fading_count} 条。"
        ),
    ]
    lines.extend(f"- {reason}" for reason in reasons[:3])
    return lines


def _format_kb(size_bytes: int) -> str:
    return f"{size_bytes / 1024:.1f}KB"
