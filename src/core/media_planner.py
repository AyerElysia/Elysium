"""Shared planning for validated native message media.

This module is the boundary between message attachments and LLM payload content.
It accepts canonical ``MediaAttachment`` objects first, then decodes a limited set
of legacy shapes through ``MediaAttachment.from_legacy``.  Native content is built
only from already materialized ``MediaRef`` bytes.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.core.models.media import MediaAttachment, MediaSegmentType
from src.kernel.llm import Audio, Content, Image, Text, Video
from src.kernel.llm.payload.media import MediaRef


DEFAULT_MAX_TOTAL_BYTES = 40 * 1024 * 1024
DEFAULT_MAX_ITEM_BYTES = 20 * 1024 * 1024

_SEGMENT_LABELS: dict[MediaSegmentType, str] = {
    MediaSegmentType.IMAGE: "[图片]",
    MediaSegmentType.EMOJI: "[表情包]",
    MediaSegmentType.VIDEO: "[视频]",
    MediaSegmentType.VOICE: "[语音]",
}
_LEGACY_SEGMENT_ALIASES = {"record": "voice", "audio": "voice"}
_SUPPORTED_WIRE_AUDIO_MIMES = frozenset({"audio/mpeg", "audio/wav"})
_MAIN_CONTENT_MEDIA_TYPES = frozenset({"image", "emoji", "voice", "video"})


@dataclass(frozen=True, slots=True)
class PlannedMedia:
    """One validated attachment selected for a native payload."""

    attachment: MediaAttachment
    label_type: str
    segment_type: MediaSegmentType
    fallback_label: str
    source_message_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.attachment, MediaAttachment):
            raise TypeError("PlannedMedia.attachment 必须是 MediaAttachment")
        segment_type = MediaSegmentType(self.segment_type)
        if segment_type is not self.attachment.segment_type:
            raise ValueError("PlannedMedia.segment_type 必须与 attachment 一致")
        object.__setattr__(self, "segment_type", segment_type)
        if not isinstance(self.label_type, str) or not self.label_type:
            raise ValueError("PlannedMedia.label_type 必须是非空字符串")
        if not isinstance(self.fallback_label, str) or not self.fallback_label:
            raise ValueError("PlannedMedia.fallback_label 必须是非空字符串")
        if self.source_message_id is not None and not str(self.source_message_id).strip():
            raise ValueError("PlannedMedia.source_message_id 必须是非空字符串或 None")


def _message_id(message: Any) -> str | None:
    value = getattr(message, "message_id", None)
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _message_type_value(value: Any) -> str:
    value = getattr(value, "value", value)
    return str(value or "").strip().lower()


def _legacy_segment_type(value: Any) -> str:
    normalized = _message_type_value(value)
    return _LEGACY_SEGMENT_ALIASES.get(normalized, normalized)


def _normalize_legacy_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize adapter aliases without inspecting media bytes."""
    normalized = dict(item)
    normalized["type"] = _legacy_segment_type(normalized.get("type"))

    nested = normalized.get("data")
    nested_mapping = dict(nested) if isinstance(nested, Mapping) else None
    if nested_mapping is not None:
        normalized["data"] = nested_mapping

    if not normalized.get("mime_type"):
        for source in (normalized, nested_mapping):
            if source is None:
                continue
            for key in ("content_type", "mime", "format"):
                value = source.get(key)
                if not isinstance(value, str) or not value.strip():
                    continue
                mime_type = value.strip().lower()
                if key == "format" and "/" not in mime_type:
                    if normalized["type"] == "voice":
                        mime_type = f"audio/{mime_type}"
                    elif normalized["type"] == "video":
                        mime_type = f"video/{mime_type}"
                normalized["mime_type"] = mime_type
                break
            if normalized.get("mime_type"):
                break
    return normalized


def _attachment_from_legacy(
    item: Mapping[str, Any],
    *,
    source_message_id: str | None,
    max_item_bytes: int,
) -> MediaAttachment:
    normalized = _normalize_legacy_item(item)
    return MediaAttachment.from_legacy(
        normalized,
        source_message_id=source_message_id,
        max_item_bytes=max_item_bytes,
    )


def _iter_legacy_items(message: Any) -> Iterator[Mapping[str, Any]]:
    content = getattr(message, "content", None)
    if isinstance(content, Mapping):
        media = content.get("media")
        if isinstance(media, list):
            yield from (item for item in media if isinstance(item, Mapping))

    extra = getattr(message, "extra", None)
    if isinstance(extra, Mapping):
        media = extra.get("media")
        if isinstance(media, list):
            yield from (item for item in media if isinstance(item, Mapping))

    direct_media = getattr(message, "media", None)
    if isinstance(direct_media, list):
        yield from (item for item in direct_media if isinstance(item, Mapping))

    media_type = _legacy_segment_type(getattr(message, "message_type", None))
    if media_type not in _MAIN_CONTENT_MEDIA_TYPES:
        return
    if isinstance(content, Mapping):
        if "data" in content:
            main_item = dict(content)
            main_item.setdefault("type", media_type)
            yield main_item
    elif content is not None:
        yield {"type": media_type, "data": content}


def iter_message_attachments(
    message: Any,
    *,
    max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
) -> Iterator[MediaAttachment]:
    """Yield materialized canonical attachments or bounded legacy attachments.

    A nonempty canonical list is authoritative, including when it holds detached
    descriptors.  Legacy parsing failures are intentionally silent; the input may
    contain large base64 strings and none of those strings are put into errors.
    """
    canonical = getattr(message, "attachments", None)
    if isinstance(canonical, (list, tuple)) and canonical:
        for attachment in canonical:
            if not isinstance(attachment, MediaAttachment):
                continue
            try:
                if attachment.media_ref.is_materialized:
                    yield attachment
            except Exception:
                continue
        return

    source_message_id = _message_id(message)
    for item in _iter_legacy_items(message):
        try:
            attachment = _attachment_from_legacy(
                item,
                source_message_id=source_message_id,
                max_item_bytes=max_item_bytes,
            )
            if attachment.media_ref.is_materialized:
                yield attachment
        except Exception:
            continue


def media_dedup_key(attachment: MediaAttachment) -> tuple[str, str]:
    """Return the stable `(segment_type, sha256)` attachment identity."""
    return (attachment.segment_type.value, attachment.media_ref.sha256)


def _limit(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _audio_duration_limit(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return 0.0
    try:
        limit = float(value)
    except (TypeError, ValueError):
        return None
    return limit if math.isfinite(limit) and limit >= 0 else None


def _history_messages(
    include_history: bool | Iterable[Any],
    history_messages: Iterable[Any] | None,
    history: Iterable[Any] | None,
    history_tail_messages: int | None,
) -> list[Any]:
    if isinstance(include_history, bool):
        if not include_history:
            return []
        source = history_messages if history_messages is not None else history
    else:
        source = include_history
    if source is None:
        return []
    values = list(source)
    if history_tail_messages is None:
        return values
    try:
        tail = int(history_tail_messages)
    except (TypeError, ValueError):
        return values
    return values[-tail:] if tail > 0 else []


def _label_type(segment_type: MediaSegmentType) -> str:
    return "audio" if segment_type is MediaSegmentType.VOICE else segment_type.value


def plan_media(
    messages: Iterable[Any],
    *,
    max_images: int = 4,
    max_videos: int = 1,
    max_audios: int = 2,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
    max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
    enable_image: bool = True,
    enable_emoji: bool = True,
    enable_video: bool = True,
    enable_audio: bool = True,
    audio_max_seconds: float | None = 60,
    include_history: bool | Iterable[Any] = False,
    history_messages: Iterable[Any] | None = None,
    history: Iterable[Any] | None = None,
    history_tail_messages: int | None = None,
) -> list[PlannedMedia]:
    """Select materialized attachments in message order under explicit budgets."""
    image_limit = _limit(max_images, 4)
    video_limit = _limit(max_videos, 1)
    audio_limit = _limit(max_audios, 2)
    total_limit = _limit(max_total_bytes, DEFAULT_MAX_TOTAL_BYTES)
    item_limit = _limit(max_item_bytes, DEFAULT_MAX_ITEM_BYTES)
    if total_limit <= 0 or item_limit <= 0:
        return []

    ordered_messages = list(messages)
    ordered_messages.extend(
        _history_messages(
            include_history,
            history_messages,
            history,
            history_tail_messages,
        )
    )

    planned: list[PlannedMedia] = []
    seen: set[tuple[str, str]] = set()
    image_count = video_count = audio_count = total_bytes = 0
    duration_limit = _audio_duration_limit(audio_max_seconds)

    for message in ordered_messages:
        message_id = _message_id(message)
        for attachment in iter_message_attachments(
            message,
            max_item_bytes=item_limit,
        ):
            segment_type = attachment.segment_type
            if segment_type is MediaSegmentType.IMAGE:
                if not enable_image or image_count >= image_limit:
                    continue
            elif segment_type is MediaSegmentType.EMOJI:
                if not enable_emoji or image_count >= image_limit:
                    continue
            elif segment_type is MediaSegmentType.VIDEO:
                if not enable_video or video_count >= video_limit:
                    continue
            elif segment_type is MediaSegmentType.VOICE:
                if not enable_audio or audio_count >= audio_limit:
                    continue
                duration = attachment.media_ref.duration
                if duration_limit is not None and duration is not None and duration > duration_limit:
                    continue
            else:
                continue

            ref = attachment.media_ref
            if not ref.is_materialized or ref.size_bytes > item_limit:
                continue
            if total_bytes + ref.size_bytes > total_limit:
                continue
            dedup_key = media_dedup_key(attachment)
            if dedup_key in seen:
                continue

            source_message_id = message_id or ref.source_message_id
            planned.append(
                PlannedMedia(
                    attachment=attachment,
                    label_type=_label_type(segment_type),
                    segment_type=segment_type,
                    fallback_label=_SEGMENT_LABELS[segment_type],
                    source_message_id=source_message_id,
                )
            )
            seen.add(dedup_key)
            total_bytes += ref.size_bytes
            if segment_type in {MediaSegmentType.IMAGE, MediaSegmentType.EMOJI}:
                image_count += 1
            elif segment_type is MediaSegmentType.VIDEO:
                video_count += 1
            else:
                audio_count += 1
    return planned


def _data_url(ref: MediaRef) -> str:
    if ref.data is None:
        raise ValueError("media reference is not materialized")
    encoded = base64.b64encode(ref.data).decode("ascii")
    return f"data:{ref.mime_type};base64,{encoded}"


def _fallback(item: PlannedMedia, default: str) -> Text:
    return Text(item.fallback_label or default)


def build_native_content(
    text: str,
    planned: Sequence[PlannedMedia],
    *,
    unsupported_audio_placeholder: str = "[语音消息]",
) -> list[Content]:
    """Build text and native content from validated materialized attachments."""
    content: list[Content] = []
    if text:
        content.append(Text(text))

    for item in planned:
        try:
            attachment = item.attachment
            ref = attachment.media_ref
            segment_type = item.segment_type
            if not ref.is_materialized:
                raise ValueError("media reference is not materialized")

            if segment_type is MediaSegmentType.VOICE:
                if ref.mime_type not in _SUPPORTED_WIRE_AUDIO_MIMES:
                    content.append(Text(unsupported_audio_placeholder))
                    continue
                native_part: Content = Audio(_data_url(ref))
            elif segment_type in {MediaSegmentType.IMAGE, MediaSegmentType.EMOJI}:
                native_part = Image(_data_url(ref))
            elif segment_type is MediaSegmentType.VIDEO:
                native_part = Video(_data_url(ref))
            else:
                content.append(_fallback(item, "[媒体消息]"))
                continue

            content.append(Text(item.fallback_label or _SEGMENT_LABELS[segment_type]))
            content.append(native_part)
        except Exception:
            content.append(_fallback(item, "[媒体消息]"))
    return content


def build_media_text(
    text: str,
    planned: Sequence[PlannedMedia],
    *,
    labels: Mapping[str, str] | None = None,
    image_label: str = "[图片]",
    emoji_label: str = "[表情包]",
    video_label: str = "[视频]",
    audio_label: str = "[语音]",
    unsupported_audio_placeholder: str = "[语音消息]",
) -> str:
    """Return the source text plus text-only media placeholders."""
    configured = {
        "image": image_label,
        "emoji": emoji_label,
        "video": video_label,
        "audio": audio_label,
    }
    if labels:
        configured.update({str(key): str(value) for key, value in labels.items()})

    parts = [text] if text else []
    for item in planned:
        segment_type = item.segment_type
        if segment_type is MediaSegmentType.VOICE:
            if item.attachment.media_ref.mime_type not in _SUPPORTED_WIRE_AUDIO_MIMES:
                parts.append(unsupported_audio_placeholder)
            else:
                parts.append(configured["audio"])
        elif segment_type is MediaSegmentType.IMAGE:
            parts.append(configured["image"])
        elif segment_type is MediaSegmentType.EMOJI:
            parts.append(configured["emoji"])
        elif segment_type is MediaSegmentType.VIDEO:
            parts.append(configured["video"])
        else:
            parts.append(item.fallback_label)
    return "".join(parts)


__all__ = [
    "DEFAULT_MAX_TOTAL_BYTES",
    "DEFAULT_MAX_ITEM_BYTES",
    "PlannedMedia",
    "iter_message_attachments",
    "media_dedup_key",
    "plan_media",
    "build_native_content",
    "build_media_text",
]
