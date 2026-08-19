"""Bounded, exactly resumable projections of subject initiative content."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ..tools.bounded_projection import (
    CORE_TOOL_RESULT_MAX_BYTES,
    project_bounded_text,
)
from .contracts import InitiativeSeedView

INITIATIVE_CONTENT_PROJECTION_NAME = "initiative-seed-content"
INITIATIVE_CONTENT_PROJECTION_MAX_BYTES = CORE_TOOL_RESULT_MAX_BYTES


def initiative_seed_content(seed: InitiativeSeedView) -> str:
    """Return the complete canonical subject-authored content."""

    return json.dumps(
        {
            "public_statement": seed.current_statement,
            "related_entity_refs": list(seed.related_entity_refs),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def initiative_seed_summary(seed: InitiativeSeedView) -> dict[str, Any]:
    """Return content-neutral list metadata; full content is read separately."""

    content = initiative_seed_content(seed)
    content_bytes = content.encode("utf-8")
    return {
        "seed_id": seed.seed_id,
        "status": seed.status,
        "revision": seed.revision,
        "opened_at": seed.opened_at,
        "last_changed_at": seed.last_changed_at,
        "content_event_id": seed.content_event_id or seed.last_event_id,
        "content_revision": seed.content_revision or seed.revision,
        "content_bytes": len(content_bytes),
        "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
        "related_entity_ref_count": len(seed.related_entity_refs),
        "reencounter_at": seed.reencounter_at,
        "reencounter_revision": seed.reencounter_revision,
        "reencounter_delivered_at": seed.reencounter_delivered_at,
    }


def project_initiative_seed_content(
    seed: InitiativeSeedView,
    *,
    continuation: str = "",
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Project exact canonical content in stable 8KiB-or-smaller chunks."""

    content = initiative_seed_content(seed)
    summary = initiative_seed_summary(seed)
    return project_bounded_text(
        projection_name=INITIATIVE_CONTENT_PROJECTION_NAME,
        # A fixed bucket keeps continuation tokens portable across heartbeat,
        # chat, voice, livestream, and embodied consciousness instances.
        task_name="core",
        requested_max_bytes=max_bytes,
        binding={
            "seed_id": seed.seed_id,
            "content_revision": summary["content_revision"],
        },
        frontier={
            "content_event_id": summary["content_event_id"],
            "content_sha256": summary["content_sha256"],
        },
        base_payload={
            "authority": "subject_initiative",
            "seed_id": seed.seed_id,
            "seed_revision": seed.revision,
            "status": seed.status,
            "content_event_id": summary["content_event_id"],
            "content_revision": summary["content_revision"],
            "content_format": "canonical_json",
            "content_sha256": summary["content_sha256"],
            "action_required": False,
        },
        content=content,
        content_ref=str(summary["content_event_id"]),
        continuation=continuation,
    )


__all__ = [
    "INITIATIVE_CONTENT_PROJECTION_MAX_BYTES",
    "INITIATIVE_CONTENT_PROJECTION_NAME",
    "initiative_seed_content",
    "initiative_seed_summary",
    "project_initiative_seed_content",
]
