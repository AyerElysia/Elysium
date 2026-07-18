"""life_engine native multimodal compatibility wrapper.

The shared planner owns canonical attachment selection and native payload creation.
This module keeps the historical ``MediaItem``/``MediaBudget`` API used by
LifeChatter and older plugins, including the ability to retain malformed legacy
items until build time where they become text placeholders.
"""

from __future__ import annotations

import base64
import hashlib
import io
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.core.media_planner import (
    PlannedMedia,
    build_native_content,
    iter_message_attachments,
    media_dedup_key,
    plan_media,
)
from src.core.models.media import MediaAttachment, MediaSegmentType
from src.kernel.llm import Audio, Content, Image, Text, Video

if TYPE_CHECKING:
    from src.core.models.message import Message


_DEFAULT_VOICE_MIME = "audio/wav"
_SUPPORTED_AUDIO_MIMES: frozenset[str] = frozenset(
    {
        "audio/mpeg",
        "audio/mp3",
        "audio/wav",
        "audio/x-wav",
        "audio/wave",
        "audio/vnd.wave",
    }
)
_VOICE_TYPES: frozenset[str] = frozenset({"voice", "record", "audio"})
_IMAGE_TYPES: frozenset[str] = frozenset({"image", "emoji"})
_SUPPORTED_IMAGE_MIMES: frozenset[str] = frozenset(
    {"image/bmp", "image/gif", "image/jpeg", "image/jpg", "image/png", "image/webp"}
)
_TYPE_ALIASES = {"record": "voice", "audio": "voice"}


class _StableMediaData(str):
    """String-compatible legacy view with a deterministic media identity hash."""

    __slots__ = ("_stable_hash",)

    def __new__(
        cls,
        value: str,
        segment_type: MediaSegmentType | None = None,
        sha256: str | None = None,
    ) -> "_StableMediaData":
        instance = str.__new__(cls, value)
        identity = f"{segment_type.value if segment_type is not None else ''}:{sha256 or value}"
        digest = hashlib.sha256(identity.encode("utf-8")).digest()
        instance._stable_hash = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
        return instance

    def __hash__(self) -> int:
        return self._stable_hash


@dataclass
class MediaItem:
    """Legacy extracted media item.

    ``raw_data`` remains for callers that inspect extraction results.  Native
    construction never trusts it directly; valid items also carry ``attachment``.
    """

    media_type: str
    raw_data: str
    source_message_id: str
    mime_type: str = ""
    duration_seconds: float = 0.0
    attachment: MediaAttachment | None = None
    fallback_label: str = ""
    segment_type: MediaSegmentType | None = None


@dataclass
class MediaBudget:
    """Three independent modality quotas plus byte budgets."""

    max_images: int = 4
    max_videos: int = 1
    max_audios: int = 2
    max_total_bytes: int = 40 * 1024 * 1024
    max_item_bytes: int = 20 * 1024 * 1024
    _images: int = field(default=0, init=False)
    _videos: int = field(default=0, init=False)
    _audios: int = field(default=0, init=False)
    _total_bytes: int = field(default=0, init=False)
    _seen_keys: set[tuple[str, str]] = field(default_factory=set, init=False)

    @staticmethod
    def _normalized_type(media_type: str) -> str:
        value = str(media_type or "").lower()
        if value in {"voice", "record", "audio"}:
            return "audio"
        return value

    def can_take(self, media_type: str, size_bytes: int = 0) -> bool:
        """Check a quota without consuming it."""
        normalized = self._normalized_type(media_type)
        if normalized in _IMAGE_TYPES:
            count_available = self._images < max(0, int(self.max_images))
        elif normalized == "video":
            count_available = self._videos < max(0, int(self.max_videos))
        elif normalized == "audio":
            count_available = self._audios < max(0, int(self.max_audios))
        else:
            return False
        try:
            size = int(size_bytes)
        except (TypeError, ValueError):
            return False
        return (
            count_available
            and 0 <= size <= max(0, int(self.max_item_bytes))
            and self._total_bytes + size <= max(0, int(self.max_total_bytes))
        )

    def consume(
        self,
        media_type: str,
        size_bytes: int = 0,
        dedup_key: tuple[str, str] | None = None,
    ) -> bool:
        """Consume a quota; return False when quota, bytes, or dedup rejects it."""
        if dedup_key is not None and dedup_key in self._seen_keys:
            return False
        if not self.can_take(media_type, size_bytes):
            return False
        normalized = self._normalized_type(media_type)
        if normalized in _IMAGE_TYPES:
            self._images += 1
        elif normalized == "video":
            self._videos += 1
        elif normalized == "audio":
            self._audios += 1
        self._total_bytes += int(size_bytes)
        if dedup_key is not None:
            self._seen_keys.add(dedup_key)
        return True

    def is_exhausted(self) -> bool:
        return (
            self._images >= max(0, int(self.max_images))
            and self._videos >= max(0, int(self.max_videos))
            and self._audios >= max(0, int(self.max_audios))
        )


def _message_id(msg: Any) -> str:
    return str(getattr(msg, "message_id", "") or "")


def _message_type_value(value: Any) -> str:
    value = getattr(value, "value", value)
    return str(value or "").strip().lower()


def _normalize_type(value: Any) -> str:
    normalized = _message_type_value(value)
    return _TYPE_ALIASES.get(normalized, normalized)


def _read_str(d: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = d.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_float(d: Mapping[str, Any], keys: tuple[str, ...]) -> float:
    nested = d.get("data")
    sources = (d, nested) if isinstance(nested, Mapping) else (d,)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                result = float(value)
            elif isinstance(value, str):
                try:
                    result = float(value.strip())
                except ValueError:
                    continue
            else:
                continue
            if math.isfinite(result) and result > 0:
                return result
    return 0.0


def _classify_media(
    media_type_raw: str,
    media: dict[str, Any],
) -> tuple[str | None, str, float]:
    """Normalize a legacy type and read its declared MIME/duration."""
    media_type = _normalize_type(media_type_raw)
    if media_type in _IMAGE_TYPES:
        mime = _read_str(media, ("mime", "mime_type", "format"))
        return media_type, mime, 0.0
    if media_type == "video":
        mime = _read_str(media, ("mime", "mime_type", "format")) or "video/mp4"
        return "video", mime, _read_float(media, ("duration", "duration_seconds"))
    if media_type == "voice":
        mime = _read_str(media, ("mime", "mime_type", "format")) or _DEFAULT_VOICE_MIME
        if "/" not in mime:
            mime = f"audio/{mime.lower()}"
        return "audio", mime.lower(), _read_float(media, ("duration", "duration_seconds"))
    return None, "", 0.0


def _extract_media_data(media_type: str, raw_data: Any) -> str:
    """Extract a legacy display source without making it a native payload."""
    if isinstance(raw_data, str):
        return _normalize_multimodal_media_data(raw_data)
    if isinstance(raw_data, Mapping):
        if media_type == "video":
            keys = ("base64", "data", "video_base64", "url", "path", "file")
        elif media_type == "audio":
            keys = ("base64", "data", "audio_base64", "url", "path", "file")
        else:
            keys = ("data", "base64", "url", "path", "file")
        for key in keys:
            value = raw_data.get(key)
            if isinstance(value, str) and value.strip():
                return _normalize_multimodal_media_data(value)
        nested = raw_data.get("media")
        if isinstance(nested, list):
            for nested_item in nested:
                if not isinstance(nested_item, Mapping):
                    continue
                for key in ("data", "base64", "url", "path", "file"):
                    value = nested_item.get(key)
                    if isinstance(value, str) and value.strip():
                        return _normalize_multimodal_media_data(value)
    return ""


def _normalize_multimodal_media_data(value: str) -> str:
    if value.startswith("base64://"):
        return f"base64|{value[len('base64://'):]}"
    return value


def _legacy_for_attachment(media: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(media)
    normalized["type"] = _normalize_type(normalized.get("type"))
    if not normalized.get("mime_type"):
        for key in ("content_type", "mime", "format"):
            value = normalized.get(key)
            if isinstance(value, str) and value.strip():
                value = value.strip().lower()
                if key == "format" and "/" not in value:
                    value = f"audio/{value}" if normalized["type"] == "voice" else value
                normalized["mime_type"] = value
                break
    return normalized


def _parse_legacy_attachment(
    media: Mapping[str, Any],
    source_message_id: str,
    *,
    max_item_bytes: int = 20 * 1024 * 1024,
) -> MediaAttachment:
    return MediaAttachment.from_legacy(
        _legacy_for_attachment(media),
        source_message_id=source_message_id or None,
        max_item_bytes=max_item_bytes,
    )


def _item_fallback_label(media_type: str, source_message_id: str) -> str:
    if media_type in {"image", "emoji"}:
        label = "表情包" if media_type == "emoji" else "图片"
        return f"[{label}:格式不支持，已跳过原生视觉输入]"
    if media_type == "video":
        return f"[视频:{source_message_id}]" if source_message_id else "[视频]"
    return "[语音消息]"


def _make_item(
    media_type: str,
    raw_data: str,
    source_message_id: str,
    *,
    mime_type: str = "",
    duration: float = 0.0,
    attachment: MediaAttachment | None = None,
    fallback_label: str = "",
) -> MediaItem:
    stable_hash = attachment.media_ref.sha256 if attachment is not None else None
    segment_type = attachment.segment_type if attachment is not None else {
        "image": MediaSegmentType.IMAGE,
        "emoji": MediaSegmentType.EMOJI,
        "video": MediaSegmentType.VIDEO,
        "audio": MediaSegmentType.VOICE,
    }.get(media_type)
    return MediaItem(
        media_type=media_type,
        raw_data=_StableMediaData(raw_data, segment_type, stable_hash),
        source_message_id=source_message_id,
        mime_type=mime_type,
        duration_seconds=duration,
        attachment=attachment,
        fallback_label=fallback_label,
        segment_type=segment_type,
    )


def _ref_data_url(attachment: MediaAttachment) -> str:
    ref = attachment.media_ref
    assert ref.data is not None
    encoded = base64.b64encode(ref.data).decode("ascii")
    return f"data:{ref.mime_type};base64,{encoded}"


def _item_from_planned(planned: PlannedMedia) -> MediaItem:
    ref = planned.attachment.media_ref
    assert ref.data is not None
    source_message_id = planned.source_message_id or ref.source_message_id or ""
    media_type = "audio" if planned.segment_type is MediaSegmentType.VOICE else planned.segment_type.value
    fallback_label = planned.fallback_label
    if source_message_id and planned.segment_type in {
        MediaSegmentType.IMAGE,
        MediaSegmentType.EMOJI,
    }:
        base_label = "[表情包]" if planned.segment_type is MediaSegmentType.EMOJI else "[图片]"
        fallback_label = f"{base_label}:{source_message_id}"
    return _make_item(
        media_type,
        _ref_data_url(planned.attachment),
        source_message_id,
        mime_type=ref.mime_type,
        duration=float(ref.duration or 0.0),
        attachment=planned.attachment,
        fallback_label=fallback_label,
    )


def _selected_plans(
    messages: list[Message],
    budget: MediaBudget,
    *,
    enable_image: bool,
    enable_emoji: bool,
    enable_video: bool,
    enable_audio: bool,
    audio_max_seconds: int,
) -> dict[int, list[PlannedMedia]]:
    selected = plan_media(
        messages,
        max_images=max(0, budget.max_images - budget._images),
        max_videos=max(0, budget.max_videos - budget._videos),
        max_audios=max(0, budget.max_audios - budget._audios),
        max_total_bytes=max(0, budget.max_total_bytes - budget._total_bytes),
        max_item_bytes=budget.max_item_bytes,
        enable_image=enable_image,
        enable_emoji=enable_emoji,
        enable_video=enable_video,
        enable_audio=enable_audio,
        audio_max_seconds=audio_max_seconds,
    )
    selected_by_key: dict[tuple[str, str], list[PlannedMedia]] = {}
    for item in selected:
        selected_by_key.setdefault(media_dedup_key(item.attachment), []).append(item)

    by_message: dict[int, list[PlannedMedia]] = {}
    for message in messages:
        for attachment in iter_message_attachments(
            message,
            max_item_bytes=budget.max_item_bytes,
        ):
            candidates = selected_by_key.get(media_dedup_key(attachment))
            if candidates:
                by_message.setdefault(id(message), []).append(candidates.pop(0))
    return by_message


def _consume_planned(item: PlannedMedia, budget: MediaBudget) -> bool:
    media_type = "audio" if item.segment_type is MediaSegmentType.VOICE else item.segment_type.value
    return budget.consume(
        media_type,
        item.attachment.media_ref.size_bytes,
        dedup_key=media_dedup_key(item.attachment),
    )


def extract_media_from_messages(
    messages: list["Message"],
    budget: MediaBudget,
    *,
    enable_image: bool = True,
    enable_emoji: bool = True,
    enable_video: bool = True,
    enable_audio: bool = True,
    audio_max_seconds: int = 60,
) -> list[MediaItem]:
    """Extract planned canonical media while retaining invalid legacy items."""
    messages = list(messages)
    selected_by_message = _selected_plans(
        messages,
        budget,
        enable_image=enable_image,
        enable_emoji=enable_emoji,
        enable_video=enable_video,
        enable_audio=enable_audio,
        audio_max_seconds=audio_max_seconds,
    )
    result: list[MediaItem] = []

    for message in messages:
        canonical = getattr(message, "attachments", None)
        if isinstance(canonical, (list, tuple)) and canonical:
            for planned in selected_by_message.get(id(message), []):
                if _consume_planned(planned, budget):
                    result.append(_item_from_planned(planned))
            continue

        selected = selected_by_message.get(id(message), [])
        selected_by_key = {media_dedup_key(item.attachment): item for item in selected}
        used_keys: set[tuple[str, str]] = set()
        raw_items = get_media_list(message)
        for media in raw_items:
            raw_type = str(media.get("type", "")).lower()
            media_type, mime_type, duration = _classify_media(raw_type, media)
            if media_type is None:
                continue
            if media_type == "image" and not enable_image:
                continue
            if media_type == "emoji" and not enable_emoji:
                continue
            if media_type == "video" and not enable_video:
                continue
            if media_type == "audio" and not enable_audio:
                continue
            if media_type == "audio" and duration > audio_max_seconds:
                continue

            try:
                attachment = _parse_legacy_attachment(media, _message_id(message), max_item_bytes=budget.max_item_bytes)
                key = media_dedup_key(attachment)
            except Exception:
                attachment = None
                key = None

            if attachment is not None:
                planned = selected_by_key.get(key)
                if planned is None or key in used_keys:
                    continue
                if not budget.consume(
                    media_type,
                    attachment.media_ref.size_bytes,
                    dedup_key=key,
                ):
                    continue
                used_keys.add(key)
                result.append(_item_from_planned(planned))
                continue

            # Compatibility only: keep malformed legacy values in extraction so
            # callers can still show a placeholder.  Build never trusts this data.
            raw_data = _extract_media_data(media_type, media.get("data", ""))
            if not raw_data:
                continue
            if not budget.consume(media_type):
                continue
            result.append(
                _make_item(
                    media_type,
                    raw_data,
                    _message_id(message),
                    mime_type=mime_type,
                    duration=duration,
                    fallback_label=_item_fallback_label(media_type, _message_id(message)),
                )
            )
    return result


def _item_to_planned(item: MediaItem) -> PlannedMedia | None:
    attachment = item.attachment
    if attachment is None:
        try:
            attachment = _parse_legacy_attachment(
                {
                    "type": "voice" if item.media_type == "audio" else item.media_type,
                    "data": str(item.raw_data),
                    "mime_type": item.mime_type or None,
                    "duration": item.duration_seconds or None,
                },
                item.source_message_id,
            )
        except Exception:
            return None
    if not attachment.media_ref.is_materialized:
        return None
    segment_type = attachment.segment_type
    if segment_type in {MediaSegmentType.IMAGE, MediaSegmentType.EMOJI}:
        raw_data = str(item.raw_data or "")
        if _is_gif_image(raw_data, item.mime_type or attachment.media_ref.mime_type):
            converted_data = _convert_gif_to_png(raw_data)
            if converted_data != raw_data:
                try:
                    attachment = _parse_legacy_attachment(
                        {
                            "type": segment_type.value,
                            "data": converted_data,
                        },
                        item.source_message_id,
                    )
                except Exception:
                    pass
    default_label = {
        MediaSegmentType.IMAGE: "[图片]",
        MediaSegmentType.EMOJI: "[表情包]",
        MediaSegmentType.VIDEO: "[视频]",
        MediaSegmentType.VOICE: "[语音]",
    }.get(segment_type, "[媒体消息]")
    fallback = item.fallback_label or (
        f"{default_label}:{item.source_message_id}" if item.source_message_id else default_label
    )
    return PlannedMedia(
        attachment=attachment,
        label_type="audio" if segment_type is MediaSegmentType.VOICE else segment_type.value,
        segment_type=segment_type,
        fallback_label=fallback,
        source_message_id=item.source_message_id or attachment.media_ref.source_message_id,
    )


def build_multimodal_content(
    text: str,
    media_items: list[MediaItem],
    *,
    unsupported_audio_placeholder: str = "[语音消息]",
) -> list[Content]:
    """Build native content; malformed old items become text placeholders."""
    content: list[Content] = [Text(text)] if text else []
    for item in media_items:
        planned = _item_to_planned(item)
        if planned is None:
            if item.media_type == "audio":
                content.append(Text(unsupported_audio_placeholder))
            else:
                content.append(Text(item.fallback_label or _item_fallback_label(item.media_type, item.source_message_id)))
            continue
        content.extend(
            build_native_content(
                "",
                [planned],
                unsupported_audio_placeholder=unsupported_audio_placeholder,
            )
        )
    return content


def get_media_list(msg: "Message") -> list[dict[str, Any]]:
    """Return legacy-compatible media dictionaries with stable deduplication."""
    canonical = getattr(msg, "attachments", None)
    if isinstance(canonical, (list, tuple)) and canonical:
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for attachment in canonical:
            if not isinstance(attachment, MediaAttachment):
                continue
            try:
                if not attachment.media_ref.is_materialized:
                    continue
                key = media_dedup_key(attachment)
                if key in seen:
                    continue
                seen.add(key)
                result.append(attachment.to_legacy())
            except Exception:
                continue
        return result

    collected: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    sources: list[Any] = []
    content = getattr(msg, "content", None)
    if isinstance(content, Mapping):
        sources.append(content.get("media"))
    extra = getattr(msg, "extra", None)
    if isinstance(extra, Mapping):
        sources.append(extra.get("media"))
    sources.append(getattr(msg, "media", None))

    for source in sources:
        if not isinstance(source, list):
            continue
        for raw_item in source:
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            media_type = _normalize_type(item.get("type"))
            if not media_type:
                continue
            item["type"] = media_type
            raw_key = item.get("data") or item.get("base64") or item.get("path") or item.get("url") or item.get("file")
            try:
                attachment = _parse_legacy_attachment(item, _message_id(msg))
                key = media_dedup_key(attachment)
            except Exception:
                key = (media_type, str(raw_key))
            if key in seen:
                continue
            seen.add(key)
            collected.append(item)

    if collected:
        return collected

    msg_type = _normalize_type(getattr(msg, "message_type", None))
    if msg_type in {"image", "emoji"} and isinstance(content, str) and content:
        data = content if content.startswith(("base64|", "data:")) else f"base64|{content}"
        return [{"type": msg_type, "data": data}]
    return []


def _is_supported_image_data(value: str) -> bool:
    """Check a legacy image through strict signature validation."""
    try:
        attachment = MediaAttachment.from_legacy({"type": "image", "data": value})
    except Exception:
        return False
    return attachment.media_ref.mime_type in _SUPPORTED_IMAGE_MIMES


def _decode_base64(value: str) -> bytes | None:
    try:
        cleaned = value.replace("\n", "").replace("\r", "").replace(" ", "")
        return base64.b64decode(cleaned, validate=True)
    except Exception:
        return None


def _decode_inline_image_data(value: str) -> bytes | None:
    data = str(value or "").strip()
    if data.startswith("data:"):
        header, separator, payload = data.partition(",")
        if not separator or ";base64" not in header.lower():
            return None
        return _decode_base64(payload.strip())
    if data.startswith("base64|"):
        data = data.split("|", 1)[1].strip()
    return _decode_base64(data)


def _detect_supported_image_mime(raw: bytes) -> str | None:
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if raw.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if raw.startswith(b"BM"):
        return "image/bmp"
    if len(raw) >= 12 and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_gif_image(data: str, mime_type: str | None) -> bool:
    declared_mime = str(mime_type or "").strip().lower()
    if declared_mime == "image/gif":
        return True
    inline_data = str(data or "").strip()
    if inline_data.startswith("data:") and inline_data[5:].split(";", 1)[0].strip().lower() == "image/gif":
        return True
    raw = _decode_inline_image_data(inline_data)
    return bool(raw and _detect_supported_image_mime(raw) == "image/gif")


def _convert_gif_to_png(image_data: str) -> str:
    """Convert the first GIF frame to raw PNG base64 when Pillow is available."""
    original_data = image_data
    try:
        from PIL import Image as PILImage

        image_bytes = _decode_inline_image_data(image_data)
        if not image_bytes:
            return original_data
        with io.BytesIO(image_bytes) as input_buffer:
            with PILImage.open(input_buffer) as img:
                if getattr(img, "n_frames", 1) > 1:
                    img.seek(0)
                if img.mode in ("RGBA", "LA", "P"):
                    background = PILImage.new("RGB", img.size, (255, 255, 255))
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    mask = img.split()[-1] if img.mode == "RGBA" else None
                    background.paste(img, mask=mask)
                    img = background
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                with io.BytesIO() as output_buffer:
                    img.save(output_buffer, format="PNG")
                    return base64.b64encode(output_buffer.getvalue()).decode("ascii")
    except Exception:
        return original_data


__all__ = [
    "MediaItem",
    "MediaBudget",
    "extract_media_from_messages",
    "build_multimodal_content",
    "get_media_list",
    "_is_supported_image_data",
    "_is_gif_image",
    "_convert_gif_to_png",
]
