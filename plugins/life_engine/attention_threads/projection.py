"""Bounded traceable projections of subject-level attention threads."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from html import escape

from .contracts import (
    ATTENTION_THREAD_MAX_PAGE_BYTES,
    ATTENTION_THREAD_MIN_PAGE_BYTES,
    AttentionThreadPage,
    AttentionThreadProjectionItem,
    AttentionThreadView,
    InstanceFocus,
)

ATTENTION_THREAD_PROJECTION_ALGORITHM = "attention-thread-ref-v1"
_WRAPPER_RESERVE_BYTES = 512
_EXCERPT_MAX_BYTES = 1024


def _utf8_prefix(value: str, max_bytes: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value, True
    chunk = encoded[:max_bytes]
    while chunk:
        try:
            return chunk.decode("utf-8"), False
        except UnicodeDecodeError:
            chunk = chunk[:-1]
    return "", False


def _render_item(item: AttentionThreadProjectionItem) -> str:
    complete = "true" if item.excerpt_complete else "false"
    return (
        f'- thread_ref="{escape(item.thread_id, quote=True)}" '
        f'status="{item.status}" '
        f'revision="{item.revision}" event_position="{item.last_event_position}" '
        f'statement_ref="{escape(item.statement_event_id, quote=True)}" '
        f'statement_sha256="{item.statement_sha256}" '
        f'statement_bytes="{item.statement_bytes}" '
        f'excerpt_complete="{complete}"\n'
        f"  {escape(item.statement_excerpt, quote=False)}"
    )


def build_attention_thread_projection(
    views: Sequence[AttentionThreadView],
    *,
    source_frontier: int,
    projection_revision: int,
    max_bytes: int = 32 * 1024,
    continuation: str = "",
    projection_kind: str = "default",
    focus: InstanceFocus | None = None,
) -> AttentionThreadPage:
    """Project stable refs without constructing an unbounded full-text prompt."""

    budget = int(max_bytes)
    if not ATTENTION_THREAD_MIN_PAGE_BYTES <= budget <= ATTENTION_THREAD_MAX_PAGE_BYTES:
        raise ValueError("attention projection byte budget is outside supported range")
    if source_frontier < 0 or projection_revision < 0:
        raise ValueError("attention projection frontier/revision must not be negative")
    projection_kind = str(projection_kind or "").strip()
    if not projection_kind or len(projection_kind) > 64:
        raise ValueError("attention projection_kind is invalid")

    ordered = sorted(
        views,
        key=lambda view: (-view.last_event_position, view.thread_id),
    )
    header_fields = [
        f'algorithm="{ATTENTION_THREAD_PROJECTION_ALGORITHM}"',
        f'source_frontier="{source_frontier}"',
        f'projection_revision="{projection_revision}"',
        f'projection_kind="{escape(projection_kind, quote=True)}"',
    ]
    if focus is not None:
        header_fields.extend(
            (
                f'focus_instance="{escape(focus.instance_id, quote=True)}"',
                f'focus_thread_ref="{escape(focus.thread_id, quote=True)}"',
                (
                    "focus_source_occurrence="
                    f'"{escape(focus.source_occurrence_id, quote=True)}"'
                ),
            )
        )
    header = "<attention_threads " + " ".join(header_fields) + ">"
    footer = "</attention_threads>"
    used = len((header + "\n" + footer).encode("utf-8"))
    available = max(0, budget - _WRAPPER_RESERVE_BYTES)
    delivered: list[AttentionThreadProjectionItem] = []
    lines: list[str] = []
    original_bytes = used
    for view in ordered:
        excerpt, complete = _utf8_prefix(
            view.current_statement,
            _EXCERPT_MAX_BYTES,
        )
        item = AttentionThreadProjectionItem(
            thread_id=view.thread_id,
            status=view.status,
            revision=view.revision,
            last_event_position=view.last_event_position,
            statement_event_id=view.statement_event_id,
            statement_sha256=view.statement_sha256,
            statement_bytes=view.statement_bytes,
            statement_excerpt=excerpt,
            excerpt_bytes=len(excerpt.encode("utf-8")),
            excerpt_complete=complete,
        )
        line = _render_item(item)
        line_bytes = len((line + "\n").encode("utf-8"))
        original_bytes += line_bytes + max(
            0,
            view.statement_bytes - item.excerpt_bytes,
        )
        if len(delivered) >= 100 or used + line_bytes > available:
            continue
        delivered.append(item)
        lines.append(line)
        used += line_bytes

    omitted_count = len(ordered) - len(delivered)
    rendered = "\n".join((header, *lines, footer))
    delivered_bytes = len(rendered.encode("utf-8"))
    if delivered_bytes > budget:
        raise RuntimeError("attention projection exceeded its hard byte budget")
    digest_material = {
        "algorithm": ATTENTION_THREAD_PROJECTION_ALGORITHM,
        "source_frontier": source_frontier,
        "projection_revision": projection_revision,
        "projection_kind": projection_kind,
        "continuation": continuation,
        "content_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    }
    projection_sha256 = hashlib.sha256(
        json.dumps(
            digest_material,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AttentionThreadPage(
        items=tuple(delivered),
        source_frontier=source_frontier,
        projection_revision=projection_revision,
        projection_sha256=projection_sha256,
        algorithm_version=ATTENTION_THREAD_PROJECTION_ALGORITHM,
        projection_kind=projection_kind,
        original_bytes=original_bytes,
        delivered_bytes=delivered_bytes,
        omitted_count=omitted_count,
        continuation=continuation,
        content=rendered,
    )


__all__ = [
    "ATTENTION_THREAD_PROJECTION_ALGORITHM",
    "build_attention_thread_projection",
]
