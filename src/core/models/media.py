"""Core message media attachment models.

Attachments keep validated media bytes at the runtime boundary while exposing only
JSON-safe descriptors from message serialization.
"""

from __future__ import annotations

import base64
import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from os import PathLike
from typing import Any

from src.kernel.llm.exceptions import MediaValidationError
from src.kernel.llm.payload.media import (
    DEFAULT_MAX_ITEM_BYTES,
    MediaKind,
    MediaRef,
    redact_media_sources,
)


class MediaSegmentType(str, Enum):
    """Media segment names used by the message transport protocol."""

    IMAGE = "image"
    EMOJI = "emoji"
    VOICE = "voice"
    VIDEO = "video"
    FILE = "file"


_SEGMENT_KINDS = {
    MediaSegmentType.IMAGE: MediaKind.IMAGE,
    MediaSegmentType.EMOJI: MediaKind.IMAGE,
    MediaSegmentType.VOICE: MediaKind.AUDIO,
    MediaSegmentType.VIDEO: MediaKind.VIDEO,
    MediaSegmentType.FILE: MediaKind.FILE,
}
_ATTACHMENT_DESCRIPTOR_KEYS = {"segment_type", "media_ref", "metadata"}
_ATTACHMENT_METADATA_KEYS = {"filename", "resource_id", "storage_key"}
_INLINE_SOURCE_KEYS = (
    "data",
    "base64",
    "image_base64",
    "audio_base64",
    "voice_base64",
    "video_base64",
)
_URL_SOURCE_KEYS = ("url", "file_url", "download_url", "downloadUrl", "src")


def _first_defined(
    outer: Mapping[str, Any],
    inner: Mapping[str, Any] | None,
    keys: tuple[str, ...],
) -> Any:
    for source in (outer, inner):
        if source is None:
            continue
        for key in keys:
            if key in source and source[key] is not None:
                return source[key]
    return None


def _is_http_url(value: str) -> bool:
    normalized = value.lstrip().lower()
    return normalized.startswith("http://") or normalized.startswith("https://")


def _validate_optional_text(name: str, value: str | None) -> None:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise MediaValidationError(f"MediaAttachment.{name} 必须是非空字符串或 None")


def _parse_legacy_duration(
    outer: Mapping[str, Any],
    inner: Mapping[str, Any] | None,
) -> float | None:
    """Return the first declared duration, rejecting malformed metadata."""
    for source in (outer, inner):
        if source is None:
            continue
        for key in ("duration", "duration_seconds"):
            if key not in source or source[key] is None:
                continue
            value = source[key]
            if isinstance(value, bool):
                raise MediaValidationError(f"legacy media {key} 必须是有限非负数")
            try:
                duration = float(value)
            except (TypeError, ValueError) as exc:
                raise MediaValidationError(
                    f"legacy media {key} 必须是有限非负数"
                ) from exc
            if not math.isfinite(duration) or duration < 0:
                raise MediaValidationError(f"legacy media {key} 必须是有限非负数")
            return duration
    return None


@dataclass(frozen=True, slots=True)
class MediaAttachment:
    """A validated message attachment with optional transport metadata."""

    segment_type: MediaSegmentType
    media_ref: MediaRef
    filename: str | None = None
    resource_id: str | None = None
    storage_key: str | None = None

    def __post_init__(self) -> None:
        try:
            segment_type = MediaSegmentType(self.segment_type)
        except (TypeError, ValueError) as exc:
            raise MediaValidationError(
                f"不支持的媒体 segment_type: {self.segment_type!r}"
            ) from exc
        object.__setattr__(self, "segment_type", segment_type)

        if not isinstance(self.media_ref, MediaRef):
            raise MediaValidationError("MediaAttachment.media_ref 必须是 MediaRef")
        expected_kind = _SEGMENT_KINDS[segment_type]
        if self.media_ref.kind is not expected_kind:
            raise MediaValidationError(
                f"segment_type={segment_type.value!r} 需要 kind={expected_kind.value!r}，"
                f"实际为 {self.media_ref.kind.value!r}"
            )

        _validate_optional_text("filename", self.filename)
        _validate_optional_text("resource_id", self.resource_id)
        _validate_optional_text("storage_key", self.storage_key)

    def to_descriptor(self) -> dict[str, Any]:
        """Return a JSON-safe descriptor without inline bytes or paths."""
        descriptor: dict[str, Any] = {
            "segment_type": self.segment_type.value,
            "media_ref": self.media_ref.to_descriptor(),
        }
        metadata = {
            key: value
            for key, value in (
                ("filename", self.filename),
                ("resource_id", self.resource_id),
                ("storage_key", self.storage_key),
            )
            if value is not None
        }
        if metadata:
            descriptor["metadata"] = metadata
        return descriptor

    @classmethod
    def from_descriptor(cls, descriptor: Mapping[str, Any]) -> "MediaAttachment":
        """Build an attachment whose :class:`MediaRef` is descriptor-only."""
        if not isinstance(descriptor, Mapping):
            raise MediaValidationError("attachment descriptor 必须是映射")

        unknown_keys = set(descriptor) - _ATTACHMENT_DESCRIPTOR_KEYS
        if unknown_keys:
            names = ", ".join(sorted(map(str, unknown_keys)))
            raise MediaValidationError(
                f"attachment descriptor 包含不支持的字段: {names}"
            )
        missing_keys = {"segment_type", "media_ref"} - set(descriptor)
        if missing_keys:
            names = ", ".join(sorted(missing_keys))
            raise MediaValidationError(f"attachment descriptor 缺少字段: {names}")

        metadata_value = descriptor.get("metadata", {})
        if not isinstance(metadata_value, Mapping):
            raise MediaValidationError("attachment metadata 必须是映射")
        unknown_metadata = set(metadata_value) - _ATTACHMENT_METADATA_KEYS
        if unknown_metadata:
            names = ", ".join(sorted(map(str, unknown_metadata)))
            raise MediaValidationError(
                f"attachment metadata 包含不支持的字段: {names}"
            )

        media_descriptor = descriptor["media_ref"]
        if not isinstance(media_descriptor, Mapping):
            raise MediaValidationError("attachment media_ref 必须是 descriptor 映射")

        return cls(
            segment_type=descriptor["segment_type"],
            media_ref=MediaRef.from_descriptor(media_descriptor),
            filename=metadata_value.get("filename"),
            resource_id=metadata_value.get("resource_id"),
            storage_key=metadata_value.get("storage_key"),
        )

    @classmethod
    def from_legacy(
        cls,
        item: Mapping[str, Any],
        *,
        source_message_id: str | None = None,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        allow_managed_paths: bool = False,
    ) -> "MediaAttachment":
        """Decode a legacy ``{"type": ..., "data": ...}`` media segment.

        Adapter-facing callers use the fail-closed default: only inline bytes or
        encoded data are accepted. Internal callers may explicitly opt in to a
        managed ``PathLike`` or nested ``path``/``file`` source. Remote URLs are
        never downloaded.
        """
        if not isinstance(item, Mapping):
            raise MediaValidationError("legacy media item 必须是映射")
        if "type" not in item:
            raise MediaValidationError("legacy media item 缺少 type")
        if "data" not in item:
            raise MediaValidationError("legacy media item 缺少 data")

        try:
            segment_type = MediaSegmentType(item["type"])
        except (TypeError, ValueError) as exc:
            raise MediaValidationError(
                f"不支持的 legacy media type: {item['type']!r}"
            ) from exc
        expected_kind = _SEGMENT_KINDS[segment_type]

        raw_data = item["data"]
        data_fields = raw_data if isinstance(raw_data, Mapping) else None
        mime_type = _first_defined(
            item,
            data_fields,
            ("mime_type", "content_type"),
        )
        filename = _first_defined(item, data_fields, ("filename", "name"))
        resource_id = _first_defined(
            item,
            data_fields,
            ("resource_id", "id", "file_id"),
        )
        storage_key = _first_defined(item, data_fields, ("storage_key",))
        duration = _parse_legacy_duration(item, data_fields)

        source = raw_data
        explicit_path = False
        if data_fields is not None:
            source = None

            for key in _INLINE_SOURCE_KEYS:
                if key not in data_fields or data_fields[key] is None:
                    continue
                candidate = data_fields[key]
                if isinstance(candidate, str) and not candidate.strip():
                    continue
                source = candidate
                break

            if source is None:
                for key in ("path", "file"):
                    if key not in data_fields or data_fields[key] is None:
                        continue
                    candidate = data_fields[key]
                    if isinstance(candidate, str) and not candidate.strip():
                        continue
                    if not isinstance(candidate, (str, PathLike)):
                        raise MediaValidationError(
                            f"legacy media {key} 必须是非空路径字符串或 PathLike"
                        )
                    source = candidate
                    explicit_path = True
                    break

            if source is None:
                for key in _URL_SOURCE_KEYS:
                    if key not in data_fields or data_fields[key] is None:
                        continue
                    url_value = data_fields[key]
                    if not isinstance(url_value, str) or not url_value.strip():
                        raise MediaValidationError(
                            f"legacy media {key} 必须是非空字符串"
                        )
                    if url_value.lstrip().lower().startswith("data:"):
                        source = url_value.strip()
                        break
                    raise MediaValidationError("不允许从远程 URL 下载 legacy media")

        media_ref = cls._media_ref_from_legacy_source(
            source,
            explicit_path=explicit_path,
            allow_managed_paths=allow_managed_paths,
            kind=expected_kind,
            mime_type=mime_type,
            source_message_id=source_message_id,
            max_item_bytes=max_item_bytes,
            duration=duration,
        )
        return cls(
            segment_type=segment_type,
            media_ref=media_ref,
            filename=filename,
            resource_id=resource_id,
            storage_key=storage_key,
        )

    @staticmethod
    def _media_ref_from_legacy_source(
        source: Any,
        *,
        explicit_path: bool,
        allow_managed_paths: bool,
        kind: MediaKind,
        mime_type: str | None,
        source_message_id: str | None,
        max_item_bytes: int,
        duration: float | None,
    ) -> MediaRef:
        if source is None:
            raise MediaValidationError("legacy media 没有可用的 bytes 或显式路径 source")

        common_kwargs = {
            "kind": kind,
            "mime_type": mime_type,
            "source_message_id": source_message_id,
            "max_item_bytes": max_item_bytes,
            "duration": duration,
        }
        try:
            is_managed_path = explicit_path or (
                isinstance(source, PathLike) and not isinstance(source, str)
            )
            if is_managed_path:
                if not allow_managed_paths:
                    raise MediaValidationError(
                        "legacy media 本地路径需要显式 allow_managed_paths=True"
                    )
                if isinstance(source, str) and _is_http_url(source):
                    raise MediaValidationError("不允许从远程 URL 下载 legacy media")
                return MediaRef.from_managed_path(source, **common_kwargs)
            if isinstance(source, (bytes, bytearray, memoryview)):
                return MediaRef.from_bytes(source, **common_kwargs)
            if isinstance(source, str):
                encoded_value = source.strip()
                if not encoded_value:
                    raise MediaValidationError(
                        "legacy media 没有可用的 bytes 或显式路径 source"
                    )
                if _is_http_url(encoded_value):
                    raise MediaValidationError("不允许从远程 URL 下载 legacy media")
                if encoded_value.lower().startswith("data:"):
                    if not encoded_value.startswith("data:"):
                        encoded_value = "data:" + encoded_value[5:]
                    return MediaRef.from_data_url(encoded_value, **common_kwargs)
                return MediaRef.from_base64(encoded_value, **common_kwargs)
        except MediaValidationError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise MediaValidationError("无法读取 legacy media source") from exc

        raise MediaValidationError(
            "legacy media source 必须是 bytes、base64 字符串或显式 PathLike"
        )

    def to_legacy(self, *, include_data: bool = True) -> dict[str, Any]:
        """Return a transport-compatible legacy segment.

        Runtime bytes are encoded only when requested and materialized. A detached
        descriptor never emits a synthetic ``data`` value.
        """
        result: dict[str, Any] = {
            "type": self.segment_type.value,
            "mime_type": self.media_ref.mime_type,
        }
        if include_data and self.media_ref.data is not None:
            encoded = base64.b64encode(self.media_ref.data).decode("ascii")
            result["data"] = f"base64|{encoded}"
            if self.segment_type is MediaSegmentType.FILE:
                # The current converter looks up file payloads through ``file``.
                result["file"] = result["data"]
        if self.filename is not None:
            result["filename"] = self.filename
        if self.resource_id is not None:
            result["resource_id"] = self.resource_id
        if self.storage_key is not None:
            result["storage_key"] = self.storage_key
        return result


__all__ = ["MediaSegmentType", "MediaAttachment", "redact_media_sources"]
