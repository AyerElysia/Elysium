"""Reusable context compaction helpers for life_chatter.

分层结构（运行态与 snapshot 共用）：
- 最多一个规范 summary（USER payload，不嵌套旧 summary）
- 最近完整 conversation groups（按 USER 开组；ToolCall/ToolResult 同组不拆）
- 未闭合末尾工具链始终保留
- 旧媒体只写 descriptor，不把 base64 放进 summary
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from src.kernel.llm import (
    Audio,
    Image,
    LLMContextManager,
    LLMPayload,
    ReasoningText,
    ROLE,
    Text,
    ToolCall,
    ToolResult,
    Video,
)

DEFAULT_MAX_GROUPS = 12
DEFAULT_MAX_PART_CHARS = 360
DEFAULT_SNAPSHOT_CHAR_BUDGET = 320_000

# Runtime hierarchical compaction defaults
DEFAULT_TRIGGER_CHARS = 120_000
DEFAULT_TARGET_CHARS = 80_000
DEFAULT_MIN_RECENT_GROUPS = 2
DEFAULT_SUMMARY_MAX_CHARS = 12_000

SUMMARY_OPEN = "<compressed_life_chatter_context>"
SUMMARY_CLOSE = "</compressed_life_chatter_context>"
SUMMARY_INTRO = (
    "以下是因上下文窗口限制而压缩的旧 life_chatter 对话片段；"
    "请把它视为此前已经发生的背景，不要当作新的用户消息："
)


@dataclass(slots=True)
class ContextCompactionResult:
    """Result of hierarchical runtime/snapshot compaction."""

    triggered: bool
    before_chars: int
    after_chars: int
    payloads: list[LLMPayload]
    dropped_groups: int = 0
    summary_updated: bool = False
    target_reached: bool = True


def is_summary_payload(payload: LLMPayload) -> bool:
    """Return True only for the strict single-Text summary envelope.

    Requirements:
    - USER role
    - exactly one Text part
    - fixed intro + open tag at the absolute start
    - close tag at the absolute end
    - open/close each appear exactly once
    Ordinary user messages that merely mention the tags must not match.
    """
    if getattr(payload, "role", None) != ROLE.USER:
        return False
    content = getattr(payload, "content", None)
    if isinstance(content, Text):
        parts = [content]
    elif isinstance(content, list):
        parts = content
    else:
        return False
    if len(parts) != 1 or not isinstance(parts[0], Text):
        return False
    text = parts[0].text or ""
    prefix = f"{SUMMARY_INTRO}\n{SUMMARY_OPEN}"
    if not text.startswith(prefix) or not text.endswith(SUMMARY_CLOSE):
        return False
    if text.count(SUMMARY_OPEN) != 1 or text.count(SUMMARY_CLOSE) != 1:
        return False
    # Body must sit between the open/close markers (allow empty body).
    open_at = text.find(SUMMARY_OPEN)
    close_at = text.rfind(SUMMARY_CLOSE)
    if open_at < 0 or close_at <= open_at:
        return False
    return True


def summarize_content_part(item: object, *, max_chars: int = DEFAULT_MAX_PART_CHARS) -> str:
    # 思考痕迹不应进入压缩摘要——它只服务于当前轮次的模型连续性。
    if isinstance(item, ReasoningText):
        return ""
    if isinstance(item, Text):
        text = item.text
    elif isinstance(item, ToolCall):
        text = f"[工具调用] {item.name}({item.args})"
    elif isinstance(item, ToolResult):
        text = f"[工具结果] {item.name}: {item.value}"
    elif isinstance(item, Image):
        text = _media_descriptor("图片", item)
    elif isinstance(item, Video):
        text = _media_descriptor("视频", item)
    elif isinstance(item, Audio):
        text = _media_descriptor("语音", item)
    else:
        text = str(item)
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    # Never allow long base64-looking blobs into summaries.
    text = _scrub_base64ish(text)
    if len(text) > max_chars:
        text = text[: max_chars - 3].rstrip() + "..."
    return text


def _media_descriptor(kind: str, item: object) -> str:
    value = str(getattr(item, "value", "") or "")
    mime = str(getattr(item, "mime_type", "") or "").strip()
    length = len(value)
    # Prefer short descriptor; never embed media bytes.
    if value.startswith("data:") or length > 80:
        suffix = f" mime={mime}" if mime else ""
        return f"[{kind}]" + (f"({suffix.strip()} len={length})" if suffix else f"(len={length})")
    if mime:
        return f"[{kind} mime={mime}]"
    return f"[{kind}]"


_BASE64ISH_RE = re.compile(
    r"(?:data:[^;]+;base64,)?[A-Za-z0-9+/]{120,}={0,2}"
)


def _scrub_base64ish(text: str) -> str:
    return _BASE64ISH_RE.sub("[omitted-binary]", text)


def summarize_payload(payload: LLMPayload, *, max_part_chars: int = DEFAULT_MAX_PART_CHARS) -> str:
    role = getattr(payload, "role", None)
    role_text = getattr(role, "value", str(role))
    parts = [
        summary
        for item in (getattr(payload, "content", None) or [])
        if (summary := summarize_content_part(item, max_chars=max_part_chars))
    ]
    return f"- {role_text}: " + " | ".join(parts) if parts else ""


def extract_existing_summary_body(payload: LLMPayload | None) -> str:
    """Extract inner text of a prior summary payload for re-summarization."""
    if payload is None or not is_summary_payload(payload):
        return ""
    chunks: list[str] = []
    for part in getattr(payload, "content", None) or []:
        if isinstance(part, Text) and part.text:
            chunks.append(part.text)
    text = "\n".join(chunks)
    start = text.find(SUMMARY_OPEN)
    end = text.find(SUMMARY_CLOSE)
    if start >= 0 and end > start:
        return text[start + len(SUMMARY_OPEN) : end].strip()
    return text.strip()


def build_summary_payload(
    *,
    previous_summary_body: str,
    dropped_groups: list[list[LLMPayload]],
    max_part_chars: int = DEFAULT_MAX_PART_CHARS,
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
) -> LLMPayload:
    """Build one strict envelope, reserving space for the newest dropped context."""
    previous = previous_summary_body.replace(SUMMARY_OPEN, "").replace(SUMMARY_CLOSE, "").strip()
    new_lines: list[str] = []
    for index, group in enumerate(dropped_groups, start=1):
        new_lines.append(f"## 片段 {index}")
        new_lines.extend(
            summary
            for payload in group
            if (summary := summarize_payload(payload, max_part_chars=max_part_chars))
        )
    newest = _scrub_base64ish("\n".join(new_lines).strip())
    # New dropped material has priority. If it alone is too large, retain its tail.
    if len(newest) > summary_max_chars:
        newest = "..." + newest[-max(0, summary_max_chars - 3) :]
        previous = ""
    remaining = max(0, summary_max_chars - len(newest))
    old_section = ""
    if previous and remaining:
        heading = "## 既有背景摘要\n"
        available = max(0, remaining - len(heading) - (1 if newest else 0))
        # Prefer newest dropped material: reserve budget for it, then keep the
        # tail of prior body by trimming from the old body head when needed.
        if available <= 0:
            clipped = ""
        elif len(previous) <= available:
            clipped = previous
        else:
            clipped = "..." + previous[-(available - 3) :] if available > 3 else previous[-available:]
        if clipped:
            old_section = heading + clipped
    body = "\n".join(part for part in (old_section, newest) if part).strip()
    body = body or "- 旧上下文已省略。"
    text = f"{SUMMARY_INTRO}\n{SUMMARY_OPEN}\n{body}\n{SUMMARY_CLOSE}"
    return LLMPayload(ROLE.USER, [Text(text)])


def compress_dropped_payload_groups(
    dropped_groups: list[list[LLMPayload]],
    remaining_payloads: list[LLMPayload],
    *,
    max_groups: int = DEFAULT_MAX_GROUPS,
    max_part_chars: int = DEFAULT_MAX_PART_CHARS,
) -> list[LLMPayload]:
    """Legacy compression hook used by LLMContextManager.compression_hook."""
    del remaining_payloads
    if not dropped_groups:
        return []
    groups = dropped_groups[-max_groups:]
    lines = [
        SUMMARY_INTRO,
        SUMMARY_OPEN,
    ]
    if omitted := max(0, len(dropped_groups) - len(groups)):
        lines.append(f"- 更早的 {omitted} 组上下文已进一步省略。")
    for index, group in enumerate(groups, start=1):
        lines.append(f"## 片段 {index}")
        lines.extend(
            summary
            for payload in group
            if (summary := summarize_payload(payload, max_part_chars=max_part_chars))
        )
    lines.append(SUMMARY_CLOSE)
    return [LLMPayload(ROLE.USER, Text("\n".join(lines)))]


def split_pinned_and_tail(payloads: Sequence[LLMPayload]) -> tuple[list[LLMPayload], list[LLMPayload]]:
    """Pin only the contiguous leading SYSTEM/TOOL prefix."""
    pinned_roles = {ROLE.SYSTEM, ROLE.TOOL}
    split_at = 0
    for payload in payloads:
        if getattr(payload, "role", None) not in pinned_roles:
            break
        split_at += 1
    return list(payloads[:split_at]), list(payloads[split_at:])


def build_conversation_groups(payloads: Sequence[LLMPayload]) -> list[list[LLMPayload]]:
    """Group by USER start while preserving every intervening payload in order."""
    groups: list[list[LLMPayload]] = []
    for payload in payloads:
        if getattr(payload, "role", None) == ROLE.USER or not groups:
            groups.append([])
        groups[-1].append(payload)
    return groups


def _has_open_tool_chain(group: Sequence[LLMPayload]) -> bool:
    """True if group ends with tool activity that should not be dropped alone."""
    if not group:
        return False
    last = group[-1]
    role = getattr(last, "role", None)
    if role == ROLE.TOOL_RESULT:
        return True
    if role == ROLE.ASSISTANT:
        for part in getattr(last, "content", None) or []:
            if isinstance(part, ToolCall):
                return True
    return False


def hierarchical_compact_payloads(
    payloads: list[LLMPayload],
    *,
    estimate: Callable[[list[LLMPayload]], int],
    trigger_chars: int = DEFAULT_TRIGGER_CHARS,
    target_chars: int = DEFAULT_TARGET_CHARS,
    min_recent_groups: int = DEFAULT_MIN_RECENT_GROUPS,
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
    max_part_chars: int = DEFAULT_MAX_PART_CHARS,
    force: bool = False,
) -> ContextCompactionResult:
    """Compact to hierarchical form when over trigger (or force=True).

    Structure after compaction:
      [pinned SYSTEM/TOOL...] + [optional single summary USER] + [recent full groups]
    """
    before = estimate(payloads)
    trigger = max(1, int(trigger_chars))
    target = max(1, min(int(target_chars), trigger))
    min_recent = max(1, int(min_recent_groups))

    if not force and before <= trigger:
        return ContextCompactionResult(
            triggered=False,
            before_chars=before,
            after_chars=before,
            payloads=list(payloads),
        )

    pinned, tail = split_pinned_and_tail(payloads)
    groups = build_conversation_groups(tail)
    if not groups:
        return ContextCompactionResult(
            triggered=False,
            before_chars=before,
            after_chars=before,
            payloads=list(payloads),
        )

    existing_summary: LLMPayload | None = None
    working_groups = list(groups)
    if working_groups and len(working_groups[0]) == 1 and is_summary_payload(working_groups[0][0]):
        existing_summary = working_groups.pop(0)[0]

    if not working_groups:
        rebuilt = pinned + ([existing_summary] if existing_summary else [])
        after = estimate(rebuilt)
        return ContextCompactionResult(
            triggered=after < before,
            before_chars=before,
            after_chars=after,
            payloads=rebuilt,
            summary_updated=False,
        )

    # Never drop an open trailing tool chain; protect from the end.
    protected_tail = 0
    if _has_open_tool_chain(working_groups[-1]):
        protected_tail = 1

    keep_count = max(min_recent, protected_tail)
    keep_count = min(keep_count, len(working_groups))

    dropped: list[list[LLMPayload]] = []
    kept = list(working_groups)

    def assemble(summary: LLMPayload | None, kept_groups: list[list[LLMPayload]]) -> list[LLMPayload]:
        middle: list[LLMPayload] = []
        if summary is not None:
            middle.append(summary)
        for g in kept_groups:
            middle.extend(g)
        return pinned + middle

    prev_body = extract_existing_summary_body(existing_summary)
    summary: LLMPayload | None = existing_summary

    while len(kept) > keep_count:
        trial_dropped = dropped + [kept[0]]
        trial_summary = build_summary_payload(
            previous_summary_body=prev_body,
            dropped_groups=trial_dropped,
            max_part_chars=max_part_chars,
            summary_max_chars=summary_max_chars,
        )
        candidate_kept = kept[1:]
        trial = assemble(trial_summary, candidate_kept)
        size = estimate(trial)
        dropped = trial_dropped
        kept = candidate_kept
        summary = trial_summary
        if size <= target:
            break

    while len(kept) > max(protected_tail, 1) and estimate(assemble(summary, kept)) > target:
        trial_dropped = dropped + [kept[0]]
        trial_summary = build_summary_payload(
            previous_summary_body=prev_body,
            dropped_groups=trial_dropped,
            max_part_chars=max_part_chars,
            summary_max_chars=summary_max_chars,
        )
        kept = kept[1:]
        dropped = trial_dropped
        summary = trial_summary

    if not dropped and existing_summary is None:
        # There may be only one protected/recent group.  Still continue so a
        # media-heavy latest group can shed binary parts, or report an honest
        # target_reached=False for an indivisible huge tool result.
        summary = None

    if dropped:
        summary = build_summary_payload(
            previous_summary_body=prev_body,
            dropped_groups=dropped,
            max_part_chars=max_part_chars,
            summary_max_chars=summary_max_chars,
        )
    elif existing_summary is not None:
        summary = existing_summary

    compacted = assemble(summary, kept)
    after = estimate(compacted)
    if after > target and kept:
        # A recent group may be dominated by binary media. Preserve its text and
        # tool chain, replacing only media bytes with stable descriptors.
        replaced_group: list[LLMPayload] = []
        changed = False
        for payload in kept[-1]:
            content: list[object] = []
            for part in getattr(payload, "content", None) or []:
                if isinstance(part, (Image, Video, Audio)):
                    content.append(Text(summarize_content_part(part, max_chars=max_part_chars)))
                    changed = True
                else:
                    content.append(part)
            replaced_group.append(LLMPayload(payload.role, content))
        if changed:
            kept[-1] = replaced_group
            compacted = assemble(summary, kept)
            after = estimate(compacted)
    return ContextCompactionResult(
        triggered=True,
        before_chars=before,
        after_chars=after,
        payloads=compacted,
        dropped_groups=len(dropped),
        summary_updated=bool(dropped),
        target_reached=after <= target,
    )


def compact_payloads(
    payloads: list[LLMPayload],
    *,
    estimate: Callable[[list[LLMPayload]], int],
    char_budget: int = DEFAULT_SNAPSHOT_CHAR_BUDGET,
    max_groups: int = DEFAULT_MAX_GROUPS,
    max_part_chars: int = DEFAULT_MAX_PART_CHARS,
    trigger_chars: int | None = None,
    target_chars: int | None = None,
    min_recent_groups: int = DEFAULT_MIN_RECENT_GROUPS,
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
) -> tuple[list[LLMPayload], int, int]:
    """Compact payloads to a strict serialized-character budget.

    Uses hierarchical compaction first (trigger/target), then falls back to a
    hard envelope summary if still over ``char_budget`` (snapshot hard cap).
    """
    before = estimate(payloads)
    budget = max(1, int(char_budget))
    trig = int(trigger_chars) if trigger_chars is not None else min(DEFAULT_TRIGGER_CHARS, budget)
    targ = int(target_chars) if target_chars is not None else min(DEFAULT_TARGET_CHARS, budget)
    force = before > budget
    result = hierarchical_compact_payloads(
        payloads,
        estimate=estimate,
        trigger_chars=max(trig, 1),
        target_chars=max(1, min(targ, budget)),
        min_recent_groups=min_recent_groups,
        summary_max_chars=summary_max_chars,
        max_part_chars=max_part_chars,
        force=force or before > trig,
    )
    compacted = result.payloads
    after = estimate(compacted)
    if after <= budget:
        return compacted, before, after

    # Hard-cap fallback: single clipped summary envelope (legacy safety).
    summaries = [
        summarize_payload(payload, max_part_chars=max_part_chars)
        for payload in compacted
        if getattr(payload, "role", None) not in {ROLE.SYSTEM, ROLE.TOOL}
    ]
    summaries = [summary for summary in summaries if summary]
    tail = summaries[-max_groups:]
    omitted = max(0, len(summaries) - len(tail))
    body = "\n".join(([f"- 更早的 {omitted} 条上下文已进一步省略。"] if omitted else []) + tail)
    body = body or "- 旧上下文过大，已省略。"
    body = _scrub_base64ish(body)
    prefix = f"{SUMMARY_INTRO}\n{SUMMARY_OPEN}\n"
    suffix = f"\n{SUMMARY_CLOSE}"
    omission_marker = "\n- 其余内容已省略。"

    def candidate(length: int) -> list[LLMPayload]:
        clipped = body[:length].rstrip()
        if length < len(body):
            clipped += omission_marker
        return [LLMPayload(ROLE.USER, Text(prefix + clipped + suffix))]

    minimum = candidate(0)
    minimum_size = estimate(minimum)
    if minimum_size > budget:
        empty_size = estimate([])
        return [], before, empty_size

    low, high = 0, len(body)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate(candidate(middle)) <= budget:
            low = middle
        else:
            high = middle - 1
    compacted = candidate(low)
    return compacted, before, estimate(compacted)
