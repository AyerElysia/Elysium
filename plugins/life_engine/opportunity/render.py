"""Bounded opportunity-page rendering. Pagination is budget, not importance."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from .contracts import (
    OPPORTUNITY_PAGE_MAX_BYTES,
    OpportunityOffer,
    OpportunityPage,
    digest_payload,
    offer_identity_payload,
)

_PAGE_INTRO = """### 机会页

机会只是建议。忽略、安静结束、不调用工具都完整，不等于拒绝。
下面分两栏：你已经接住的连续性，以及当前到期的工程事实。
连续性不是系统新建议；工程事实不是任务，也不按重要性排序。"""

_CONTINUITY_HEAD = """#### 你留下的线索

查阅或改写请用 `nucleus_proactive_query` / `nucleus_proactive_command`。
看见本页不会关闭、暂停或冷却这些线索。"""

_INVITATION_HEAD = """#### 可见机会

是否动手由你决定。disclosure_ref 指向 skill 或已有常驻工具，不替你调用、不自动打开 skill 正文。"""


def clip_utf8(text: str, max_bytes: int) -> str:
    """Trim on a UTF-8 byte boundary without inventing replacement text."""

    if max_bytes <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    clipped = encoded[:max_bytes]
    while clipped:
        try:
            return clipped.decode("utf-8")
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    return ""


def utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def identity_summary(offer: OpportunityOffer) -> str:
    facts = offer.facts
    if offer.domain == "attention":
        status = str(facts.get("status") or "").strip()
        excerpt = str(facts.get("excerpt") or "").strip()
        return clip_utf8(" ".join(part for part in (status, excerpt) if part), 120)
    if offer.domain == "initiative":
        status = str(facts.get("status") or "").strip()
        excerpt = str(facts.get("excerpt") or "").strip()
        return clip_utf8(" ".join(part for part in (status, excerpt) if part), 120)
    if offer.domain == "learning":
        count = facts.get("due_count")
        return f"{count} 份主体文档到期；说明在 learning skill" if count is not None else "主体文档复盘窗口；说明在 learning skill"
    if offer.domain == "memory":
        size_bytes = facts.get("size_bytes")
        if isinstance(size_bytes, int):
            return f"MEMORY.md 约 {size_bytes / 1024:.1f} KiB 结构压力"
        return "MEMORY.md 结构压力"
    if offer.domain == "narrative":
        count = facts.get("pending_count")
        return f"{count} 条未沉淀留痕" if count is not None else "长河回望窗口"
    if offer.domain == "file_care":
        total = facts.get("total")
        return f"diaries/ 根目录 {total} 篇" if total is not None else "手写日记根目录拥挤"
    if offer.domain == "epistemic":
        question = str(facts.get("open_question") or facts.get("observed_gap") or "").strip()
        return clip_utf8(question, 120)
    return offer.domain


def identity_line(offer: OpportunityOffer) -> str:
    summary = identity_summary(offer)
    suffix = f" {summary}" if summary else ""
    return f"- [{offer.kind}/{offer.domain}] `{offer.offer_id}`{suffix}"


def facts_expansion(offer: OpportunityOffer) -> str:
    refs = ", ".join(offer.disclosure_ref)
    compact = {
        key: value
        for key, value in offer.facts.items()
        if key not in {"excerpt"}
    }
    payload = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    lines = [f"  facts: {clip_utf8(payload, 360)}"]
    if refs:
        lines.append(f"  disclosure_ref: {refs}")
    return "\n".join(lines)


def _omitted_footer(omitted_ids: list[str]) -> str:
    if not omitted_ids:
        return ""
    return "omitted: " + ", ".join(omitted_ids)


def _assemble(
    *,
    marker: str,
    continuity_lines: list[str],
    invitation_lines: list[str],
    omitted_ids: list[str],
) -> str:
    # Omitted ids sit next to the marker so a hard UTF-8 clip cannot
    # silently drop the declaration that something was left out.
    parts = [marker]
    footer = _omitted_footer(omitted_ids)
    if footer:
        parts.extend(["", footer])
    parts.extend(["", _PAGE_INTRO])
    if continuity_lines:
        parts.extend(["", _CONTINUITY_HEAD, "", *continuity_lines])
    if invitation_lines:
        parts.extend(["", _INVITATION_HEAD, "", *invitation_lines])
    return "\n".join(parts).rstrip() + "\n"


def render_opportunity_page(
    offers: tuple[OpportunityOffer, ...] | list[OpportunityOffer],
    *,
    max_bytes: int = OPPORTUNITY_PAGE_MAX_BYTES,
    observed_at: str = "",
) -> OpportunityPage | None:
    """Render identity lines first, then leftover facts. Never silent-drop."""

    ordered = tuple(sorted(offers, key=lambda item: item.sort_key()))
    if not ordered:
        return None
    budget = max(512, int(max_bytes))
    observed = observed_at or datetime.now(UTC).astimezone().isoformat()
    placeholder = "<opportunity_page delivery_id=pending>"

    shown: list[OpportunityOffer] = []
    omitted: list[str] = []
    identity_by_id: dict[str, str] = {
        offer.offer_id: identity_line(offer) for offer in ordered
    }

    def current_text(
        current_shown: list[OpportunityOffer],
        current_omitted: list[str],
        expansions: dict[str, str],
        marker: str,
    ) -> str:
        continuity: list[str] = []
        invitation: list[str] = []
        for offer in current_shown:
            line = identity_by_id[offer.offer_id]
            extra = expansions.get(offer.offer_id)
            block = line if not extra else f"{line}\n{extra}"
            if offer.kind == "continuity":
                continuity.append(block)
            else:
                invitation.append(block)
        return _assemble(
            marker=marker,
            continuity_lines=continuity,
            invitation_lines=invitation,
            omitted_ids=current_omitted,
        )

    for offer in ordered:
        trial_shown = [*shown, offer]
        trial = current_text(trial_shown, omitted, {}, placeholder)
        if utf8_size(trial) <= budget:
            shown.append(offer)
            continue
        omitted.append(offer.offer_id)
        trial_omit = current_text(shown, omitted, {}, placeholder)
        if utf8_size(trial_omit) <= budget:
            continue
        # Declaration grew past budget: drop the last shown identity instead
        # of hiding the omitted id. Identity-first still prefers earlier keys.
        if shown:
            moved = shown.pop()
            omitted.insert(0, moved.offer_id)

    expansions: dict[str, str] = {}
    for offer in shown:
        extra = facts_expansion(offer)
        trial_exp = dict(expansions)
        trial_exp[offer.offer_id] = extra
        trial = current_text(shown, omitted, trial_exp, placeholder)
        if utf8_size(trial) <= budget:
            expansions = trial_exp

    identity = digest_payload(
        {
            "omitted": omitted,
            "shown": [offer_identity_payload(offer) for offer in shown],
        }
    )
    delivery_id = f"opp_page_{identity[:32]}"
    marker = f"<opportunity_page delivery_id={delivery_id}>"
    text = current_text(shown, omitted, expansions, marker)
    if utf8_size(text) > budget:
        expansions = {}
        text = current_text(shown, omitted, expansions, marker)
    if utf8_size(text) > budget:
        text = clip_utf8(text, budget)
        if marker not in text:
            text = clip_utf8(marker + "\n" + _omitted_footer(omitted) + "\n", budget)
    return OpportunityPage(
        delivery_id=delivery_id,
        delivery_marker=marker,
        text=text,
        offers=ordered,
        shown_ids=tuple(offer.offer_id for offer in shown),
        omitted_ids=tuple(omitted),
        observed_at=observed,
    )
