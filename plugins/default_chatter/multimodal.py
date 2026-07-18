"""DefaultChatter native image compatibility wrapper."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.core.media_planner import (
    PlannedMedia,
    build_native_content,
    iter_message_attachments,
)
from src.core.models.media import MediaAttachment, MediaSegmentType
from src.core.models.message import Message
from src.kernel.llm import Content, Text
from src.kernel.llm.payload.tooling import LLMUsable


def get_image_media_list(msg: Message) -> list[dict[str, Any]]:
    """Return legacy-compatible materialized image dictionaries only."""
    canonical = getattr(msg, "attachments", None)
    if isinstance(canonical, (list, tuple)) and canonical:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for attachment in iter_message_attachments(msg):
            if attachment.segment_type is not MediaSegmentType.IMAGE:
                continue
            key = (attachment.segment_type.value, attachment.media_ref.sha256)
            if key in seen:
                continue
            seen.add(key)
            result.append(attachment.to_legacy())
        return result

    media = _read_raw_media(msg)
    return [item for item in media if item.get("type") == "image" and item.get("data")]


def extract_images_from_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Extract legacy-compatible images in message order."""
    items: list[dict[str, Any]] = []
    for msg in messages:
        items.extend(get_image_media_list(msg))
    return items


def _planned_from_item(item: Mapping[str, Any], msg: Message | None = None) -> PlannedMedia | None:
    source_message_id = str(getattr(msg, "message_id", "") or "") if msg is not None else None
    try:
        attachment = MediaAttachment.from_legacy(
            item,
            source_message_id=source_message_id or None,
        )
    except Exception:
        return None
    if attachment.segment_type is not MediaSegmentType.IMAGE or not attachment.media_ref.is_materialized:
        return None
    source_id = source_message_id or attachment.media_ref.source_message_id
    fallback = f"[图片:{source_id}]" if source_id else "[图片]"
    return PlannedMedia(
        attachment=attachment,
        label_type="image",
        segment_type=MediaSegmentType.IMAGE,
        fallback_label=fallback,
        source_message_id=source_id,
    )


def build_multimodal_content(
    text: str,
    media_items: list[dict[str, Any]],
) -> list[Content | LLMUsable]:
    """Build text plus strictly validated native images.

    Legacy malformed data is intentionally represented as a text placeholder and
    never passed to ``Image``.  ``Image`` construction receives only a validated
    attachment's canonical data URL.
    """
    content: list[Content | LLMUsable] = [Text(text)] if text else []
    for item in media_items:
        if not isinstance(item, Mapping):
            content.append(Text("[图片:格式不支持，已跳过原生视觉输入]"))
            continue
        planned = _planned_from_item(item)
        if planned is None:
            content.append(Text("[图片:格式不支持，已跳过原生视觉输入]"))
            continue
        content.extend(build_native_content("", [planned]))
    return content


# Compatibility helpers retained for callers/tests that inspect raw legacy data.
def _extract_dict_list(raw: Any) -> list[dict[str, Any]] | None:
    if isinstance(raw, list) and raw:
        return [item for item in raw if isinstance(item, dict)]
    return None


def _read_raw_media(msg: Message) -> list[dict[str, Any]]:
    """Read legacy media lists, preserving the historical source precedence."""
    content = getattr(msg, "content", None)
    if isinstance(content, Mapping):
        items = _extract_dict_list(content.get("media"))
        if items and any(item.get("data") for item in items):
            return items

    extra = getattr(msg, "extra", None)
    if isinstance(extra, Mapping):
        items = _extract_dict_list(extra.get("media"))
        if items:
            return items
    return []


__all__ = [
    "get_image_media_list",
    "extract_images_from_messages",
    "build_multimodal_content",
]
