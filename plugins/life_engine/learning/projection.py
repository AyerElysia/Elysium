"""Deterministic, traceable byte-bounded projections for learning prompts."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

LEARNING_PROJECTION_ALGORITHM = "utf8-head-tail-v1"


def _utf8_head(value: bytes, limit: int) -> str:
    if limit <= 0:
        return ""
    return value[:limit].decode("utf-8", errors="ignore")


def _utf8_tail(value: bytes, limit: int) -> str:
    if limit <= 0:
        return ""
    return value[-limit:].decode("utf-8", errors="ignore")


@dataclass(frozen=True, slots=True)
class LearningPromptProjection:
    """One delivered learning prompt layer and its content-free trace."""

    text: str
    projection_kind: str
    source_sha256: str
    original_bytes: int
    delivered_content_bytes: int
    delivered_bytes: int
    max_bytes: int
    truncated: bool
    algorithm: str = LEARNING_PROJECTION_ALGORITHM

    def stats(self) -> dict[str, Any]:
        return {
            "projection_kind": self.projection_kind,
            "algorithm": self.algorithm,
            "source_sha256": self.source_sha256,
            "original_bytes": self.original_bytes,
            "delivered_content_bytes": self.delivered_content_bytes,
            "delivered_bytes": self.delivered_bytes,
            "max_bytes": self.max_bytes,
            "truncated": self.truncated,
        }


def project_learning_text(
    source_text: str,
    *,
    max_bytes: int,
    projection_kind: str,
) -> LearningPromptProjection:
    """Project text without altering its authoritative source or UTF-8 validity."""

    source = str(source_text or "")
    kind = str(projection_kind or "learning").strip() or "learning"
    source_bytes = source.encode("utf-8")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    budget = int(max_bytes)
    if budget <= 0:
        return LearningPromptProjection(
            text=source,
            projection_kind=kind,
            source_sha256=source_sha256,
            original_bytes=len(source_bytes),
            delivered_content_bytes=len(source_bytes),
            delivered_bytes=len(source_bytes),
            max_bytes=0,
            truncated=False,
        )

    def header(delivered_content_bytes: int, truncated: bool) -> str:
        return (
            f'<learning_projection kind="{kind}" '
            f'algorithm="{LEARNING_PROJECTION_ALGORITHM}" '
            f'source_sha256="{source_sha256}" '
            f'original_bytes="{len(source_bytes)}" '
            f'delivered_content_bytes="{delivered_content_bytes}" '
            f'max_bytes="{budget}" truncated="{str(truncated).lower()}">\n'
        )

    closing = "\n</learning_projection>"
    delivered_content = source
    truncated = False
    for _ in range(3):
        prefix = header(len(delivered_content.encode("utf-8")), truncated)
        envelope_bytes = len((prefix + closing).encode("utf-8"))
        content_budget = max(0, budget - envelope_bytes)
        if len(source_bytes) <= content_budget:
            delivered_content = source
            truncated = False
            continue
        truncated = True
        marker = "\n…<bounded learning projection>…\n"
        marker_bytes = marker.encode("utf-8")
        remaining = max(0, content_budget - len(marker_bytes))
        head_budget = remaining // 2
        tail_budget = remaining - head_budget
        delivered_content = (
            _utf8_head(source_bytes, head_budget)
            + (marker if content_budget >= len(marker_bytes) else "")
            + _utf8_tail(source_bytes, tail_budget)
        )

    prefix = header(len(delivered_content.encode("utf-8")), truncated)
    rendered = prefix + delivered_content + closing
    while len(rendered.encode("utf-8")) > budget and delivered_content:
        delivered_content = _utf8_head(
            delivered_content.encode("utf-8"),
            len(delivered_content.encode("utf-8")) - 1,
        )
        prefix = header(len(delivered_content.encode("utf-8")), True)
        rendered = prefix + delivered_content + closing
        truncated = True
    if len(rendered.encode("utf-8")) > budget:
        rendered = _utf8_head(rendered.encode("utf-8"), budget)
        delivered_content = ""
        truncated = True
    return LearningPromptProjection(
        text=rendered,
        projection_kind=kind,
        source_sha256=source_sha256,
        original_bytes=len(source_bytes),
        delivered_content_bytes=len(delivered_content.encode("utf-8")),
        delivered_bytes=len(rendered.encode("utf-8")),
        max_bytes=budget,
        truncated=truncated,
    )


__all__ = [
    "LEARNING_PROJECTION_ALGORITHM",
    "LearningPromptProjection",
    "project_learning_text",
]
