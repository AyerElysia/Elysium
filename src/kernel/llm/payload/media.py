"""Typed media intermediate representation for LLM payloads.

Media is decoded and validated once at the payload boundary. Provider serializers
consume :class:`MediaRef` and never inspect filesystem paths or infer MIME types.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import math
import re
import stat as stat_module
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, dataclass, field
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import Any, ClassVar

from ..exceptions import MediaLimitError, MediaValidationError, UnsupportedModalityError


DEFAULT_MAX_ITEM_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_ITEM_BYTES = 200 * 1024 * 1024


class MediaKind(str, Enum):
    """Supported media categories."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"


_MIME_RE = re.compile(r"^[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DATA_URL_RE = re.compile(r"^data:([^,]+),(.*)$", re.DOTALL)
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/mp3": "audio/mpeg",
    "audio/mpeg3": "audio/mpeg",
    "audio/x-mpeg-3": "audio/mpeg",
    "audio/x-amr": "audio/amr",
    "audio/x-silk": "audio/silk",
}
_DESCRIPTOR_KEYS = {
    "kind",
    "mime_type",
    "size_bytes",
    "sha256",
    "source_message_id",
    "origin",
    "persistence_policy",
    "duration",
    "dimensions",
}
def _normalize_redaction_key(value: Any) -> str:
    """Normalize snake/kebab/camel-compatible media field names."""
    return str(value or "").strip().lower().replace("_", "").replace("-", "")


_MEDIA_PAYLOAD_TYPES = frozenset(
    {
        "image",
        "emoji",
        "voice",
        "audio",
        "video",
        "file",
        "imageurl",
        "inputimage",
        "inputaudio",
        "outputaudio",
        "videourl",
        "inputvideo",
    }
)
_MEDIA_CONTAINER_KEYS = frozenset({"media", "attachments", "imageurl", "source"})
_MEDIA_SOURCE_KEYS = frozenset(
    {
        "data",
        "base64",
        "imagebase64",
        "audiobase64",
        "voicebase64",
        "videobase64",
        "raw",
        "rawdata",
        "path",
        "file",
        "filepath",
        "localpath",
        "temppath",
        "url",
        "uri",
        "src",
        "fileurl",
        "downloadurl",
        "source",
    }
)
_REDACTED_MEDIA_VALUE = "[removed]"


def redact_media_sources(
    value: Any,
    *,
    media_context: bool = False,
    source_value: bool = False,
) -> Any:
    """Return a copy with media bodies, URLs, and local paths removed.

    Text and transport metadata are preserved. The helper recognizes both Neo's
    legacy media segments and common OpenAI/Anthropic request content shapes.
    """
    if isinstance(value, Mapping):
        item_type = _normalize_redaction_key(value.get("type", ""))
        current_media_context = media_context or item_type in _MEDIA_PAYLOAD_TYPES
        sanitized: dict[Any, Any] = {}
        for key, child in value.items():
            normalized_key = _normalize_redaction_key(key)
            child_media_context = (
                current_media_context or normalized_key in _MEDIA_CONTAINER_KEYS
            )
            child_is_source = (
                child_media_context and normalized_key in _MEDIA_SOURCE_KEYS
            )
            sanitized[key] = redact_media_sources(
                child,
                media_context=child_media_context,
                source_value=child_is_source,
            )
        return sanitized

    if source_value or isinstance(value, (bytes, bytearray, memoryview)):
        return _REDACTED_MEDIA_VALUE

    if isinstance(value, (list, tuple)):
        return [
            redact_media_sources(item, media_context=media_context)
            for item in value
        ]

    return value


def _validate_max_item_bytes(max_item_bytes: int) -> int:
    if (
        isinstance(max_item_bytes, bool)
        or not isinstance(max_item_bytes, int)
        or max_item_bytes <= 0
    ):
        raise MediaValidationError("max_item_bytes 必须是正整数")
    if max_item_bytes > ABSOLUTE_MAX_ITEM_BYTES:
        raise MediaLimitError(
            f"max_item_bytes {max_item_bytes} 超过绝对上限 "
            f"{ABSOLUTE_MAX_ITEM_BYTES} bytes"
        )
    return max_item_bytes


def _raise_size_limit(size_bytes: int, max_item_bytes: int) -> None:
    if size_bytes > max_item_bytes:
        raise MediaLimitError(
            f"媒体单项大小 {size_bytes} 超过限制 {max_item_bytes} bytes"
        )


def _normalize_kind(kind: MediaKind | str | None) -> MediaKind | None:
    if kind is None:
        return None
    if isinstance(kind, MediaKind):
        return kind
    try:
        return MediaKind(str(kind).lower())
    except ValueError as exc:
        raise MediaValidationError(f"不支持的媒体 kind: {kind!r}") from exc


def normalize_media_mime_type(mime_type: str) -> str:
    """Validate and canonicalize a media MIME type."""
    if not isinstance(mime_type, str):
        raise MediaValidationError("mime_type 必须是字符串")
    normalized = mime_type.strip().lower()
    if not normalized or not _MIME_RE.fullmatch(normalized):
        raise MediaValidationError(f"无效的 MIME 类型: {mime_type!r}")
    return _MIME_ALIASES.get(normalized, normalized)


def _kind_from_mime(mime_type: str) -> MediaKind:
    top_level = mime_type.split("/", 1)[0]
    if top_level == "image":
        return MediaKind.IMAGE
    if top_level == "audio":
        return MediaKind.AUDIO
    if top_level == "video":
        return MediaKind.VIDEO
    return MediaKind.FILE


def _detect_mime(data: bytes, *, kind_hint: MediaKind | None = None) -> str | None:
    """Detect common media signatures without relying on a filename.

    ISO-BMFF and EBML containers can hold either audio or video.  Their type is
    resolved from the already-declared media kind, never from a provider-specific
    filename heuristic.
    """
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    stripped = data.lstrip()
    if stripped.startswith(b"<svg") or (
        stripped.startswith(b"<?xml") and b"<svg" in stripped[:2048]
    ):
        return "image/svg+xml"

    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
        return "audio/amr"
    if data.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        return "audio/silk"
    if data.startswith(b"ID3"):
        return "audio/mpeg"
    if len(data) >= 2 and data[0] == 0xFF:
        if (data[1] & 0xF6) == 0xF0:
            return "audio/aac"
        if (data[1] & 0xE0) == 0xE0:
            return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"

    if len(data) >= 12 and data[4:8] == b"ftyp":
        return "audio/mp4" if kind_hint is MediaKind.AUDIO else "video/mp4"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"AVI ":
        return "video/x-msvideo"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        header = data[:512].lower()
        is_webm = b"webm" in header
        if kind_hint is MediaKind.AUDIO:
            return "audio/webm" if is_webm else "audio/x-matroska"
        return "video/webm" if is_webm else "video/x-matroska"
    return None


def _signature_matches(kind: MediaKind, declared_mime: str, detected_mime: str) -> bool:
    if declared_mime == detected_mime:
        return True

    # ISO-BMFF has a shared magic signature; only allow equivalent containers
    # within the declared kind rather than accepting a cross-kind MIME.
    if kind is MediaKind.AUDIO and detected_mime == "audio/mp4":
        return declared_mime in {"audio/mp4", "audio/x-m4a", "audio/m4a"}
    if kind is MediaKind.VIDEO and detected_mime == "video/mp4":
        return declared_mime in {"video/mp4", "video/quicktime"}

    # A minimal EBML header does not include tracks or a doctype.  Preserve the
    # kind boundary while allowing the two equivalent Matroska/WebM labels.
    if kind is MediaKind.AUDIO and detected_mime in {"audio/webm", "audio/x-matroska"}:
        return declared_mime in {"audio/webm", "audio/x-matroska"}
    if kind is MediaKind.VIDEO and detected_mime in {"video/webm", "video/x-matroska"}:
        return declared_mime in {"video/webm", "video/x-matroska"}

    return False


def _validate_magic(kind: MediaKind, mime_type: str, data: bytes) -> None:
    """Ensure typed media has a recognizable and matching signature."""
    if kind is MediaKind.FILE:
        return

    detected = _detect_mime(data, kind_hint=kind)
    if detected is None:
        raise MediaValidationError(
            f"无法识别 {kind.value} 媒体的文件签名，不能确认 MIME 类型 {mime_type!r}"
        )

    if _kind_from_mime(detected) is not kind:
        raise MediaValidationError(
            f"媒体 kind={kind.value!r} 与实际签名 {detected!r} 不匹配"
        )

    if not _signature_matches(kind, mime_type, detected):
        raise MediaValidationError(
            f"声明的 MIME {mime_type!r} 与实际签名 {detected!r} 不匹配"
        )


@dataclass(frozen=True, slots=True)
class MediaRef:
    """Immutable media metadata with optional validated in-memory bytes."""

    kind: MediaKind
    mime_type: str
    size_bytes: int
    sha256: str
    data: bytes | None = field(default=None, repr=False)
    source_message_id: str | None = None
    origin: str = "inline"
    persistence_policy: str = "ephemeral"
    duration: float | None = None
    dimensions: tuple[int, int] | None = None

    DEFAULT_MAX_ITEM_BYTES: ClassVar[int] = DEFAULT_MAX_ITEM_BYTES
    ABSOLUTE_MAX_ITEM_BYTES: ClassVar[int] = ABSOLUTE_MAX_ITEM_BYTES

    def __post_init__(self) -> None:
        kind = _normalize_kind(self.kind)
        if kind is None:
            raise MediaValidationError("MediaRef.kind 不能为空")
        object.__setattr__(self, "kind", kind)
        mime_type = normalize_media_mime_type(self.mime_type)
        object.__setattr__(self, "mime_type", mime_type)

        if kind is not MediaKind.FILE and _kind_from_mime(mime_type) is not kind:
            raise MediaValidationError(
                f"媒体 kind={kind.value!r} 与 MIME {mime_type!r} 不匹配"
            )
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise MediaValidationError("MediaRef.size_bytes 必须是非负整数")
        _raise_size_limit(self.size_bytes, self.ABSOLUTE_MAX_ITEM_BYTES)
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise MediaValidationError("MediaRef.sha256 必须是 64 位小写十六进制字符串")

        if self.source_message_id is not None and (
            not isinstance(self.source_message_id, str) or not self.source_message_id.strip()
        ):
            raise MediaValidationError(
                "MediaRef.source_message_id 必须是非空字符串或 None"
            )
        if not isinstance(self.origin, str) or not self.origin.strip():
            raise MediaValidationError("MediaRef.origin 必须是非空字符串")
        if (
            not isinstance(self.persistence_policy, str)
            or not self.persistence_policy.strip()
        ):
            raise MediaValidationError("MediaRef.persistence_policy 必须是非空字符串")
        if self.duration is not None and (
            isinstance(self.duration, bool)
            or not isinstance(self.duration, (int, float))
            or not math.isfinite(self.duration)
            or self.duration < 0
        ):
            raise MediaValidationError("MediaRef.duration 必须是有限非负数")
        if self.dimensions is not None and (
            not isinstance(self.dimensions, tuple)
            or len(self.dimensions) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool) and value > 0
                for value in self.dimensions
            )
        ):
            raise MediaValidationError("MediaRef.dimensions 必须是正整数二元组")

        if self.data is None:
            return
        if not isinstance(self.data, bytes):
            raise MediaValidationError("MediaRef.data 必须是 bytes 或 None")
        actual_size = len(self.data)
        _raise_size_limit(actual_size, self.ABSOLUTE_MAX_ITEM_BYTES)
        if self.size_bytes != actual_size:
            raise MediaValidationError("MediaRef.size_bytes 必须等于 data 的实际长度")
        if hashlib.sha256(self.data).hexdigest() != self.sha256:
            raise MediaValidationError("MediaRef.sha256 与 data 内容不匹配")
        _validate_magic(kind, mime_type, self.data)

    @property
    def is_materialized(self) -> bool:
        """Whether validated bytes are currently attached."""
        return self.data is not None

    def to_descriptor(self) -> dict[str, Any]:
        """Return JSON-safe metadata without bytes, base64, or filesystem paths."""
        return {
            "kind": self.kind.value,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "source_message_id": self.source_message_id,
            "origin": self.origin,
            "persistence_policy": self.persistence_policy,
            "duration": self.duration,
            "dimensions": list(self.dimensions) if self.dimensions is not None else None,
        }

    @classmethod
    def from_descriptor(cls, descriptor: Mapping[str, Any]) -> "MediaRef":
        """Build a metadata-only reference from a strict JSON-safe descriptor."""
        if not isinstance(descriptor, Mapping):
            raise MediaValidationError("媒体 descriptor 必须是映射")
        unknown_keys = set(descriptor) - _DESCRIPTOR_KEYS
        if unknown_keys:
            names = ", ".join(sorted(map(str, unknown_keys)))
            raise MediaValidationError(f"媒体 descriptor 包含不支持的字段: {names}")
        missing_keys = {"kind", "mime_type", "size_bytes", "sha256"} - set(descriptor)
        if missing_keys:
            names = ", ".join(sorted(missing_keys))
            raise MediaValidationError(f"媒体 descriptor 缺少字段: {names}")

        dimensions_value = descriptor.get("dimensions")
        dimensions: tuple[int, int] | None
        if dimensions_value is None:
            dimensions = None
        elif isinstance(dimensions_value, (list, tuple)) and len(dimensions_value) == 2:
            dimensions = (dimensions_value[0], dimensions_value[1])
        else:
            raise MediaValidationError("MediaRef.dimensions 必须是正整数二元组")

        return cls(
            kind=descriptor["kind"],
            mime_type=descriptor["mime_type"],
            size_bytes=descriptor["size_bytes"],
            sha256=descriptor["sha256"],
            data=None,
            source_message_id=descriptor.get("source_message_id"),
            origin=descriptor.get("origin", "inline"),
            persistence_policy=descriptor.get("persistence_policy", "ephemeral"),
            duration=descriptor.get("duration"),
            dimensions=dimensions,
        )

    def materialize(
        self, source: bytes | bytearray | memoryview | PathLike[str]
    ) -> "MediaRef":
        """Attach explicit bytes or bytes read from an explicit ``PathLike`` source."""
        if isinstance(source, (bytes, bytearray, memoryview)):
            source_size = source.nbytes if isinstance(source, memoryview) else len(source)
            if source_size != self.size_bytes:
                raise MediaValidationError("媒体数据大小与 descriptor 不匹配")
            data = bytes(source)
        elif isinstance(source, PathLike) and not isinstance(source, str):
            managed_path = Path(source)
            try:
                path_stat = managed_path.stat()
            except OSError as exc:
                raise MediaValidationError(
                    f"媒体文件不存在或无法访问: {managed_path}"
                ) from exc
            if not stat_module.S_ISREG(path_stat.st_mode):
                raise MediaValidationError(f"媒体文件不是普通文件: {managed_path}")
            if path_stat.st_size != self.size_bytes:
                raise MediaValidationError("媒体文件大小与 descriptor 不匹配")
            try:
                data = managed_path.read_bytes()
            except OSError as exc:
                raise MediaValidationError(f"无法读取媒体文件: {managed_path}") from exc
        else:
            raise TypeError("MediaRef.materialize 需要 bytes-like 或显式 PathLike 输入")

        return type(self)(
            kind=self.kind,
            mime_type=self.mime_type,
            size_bytes=self.size_bytes,
            sha256=self.sha256,
            data=data,
            source_message_id=self.source_message_id,
            origin=self.origin,
            persistence_policy=self.persistence_policy,
            duration=self.duration,
            dimensions=self.dimensions,
        )

    @classmethod
    def _build(
        cls,
        data: bytes,
        *,
        kind: MediaKind | str | None,
        mime_type: str | None,
        max_item_bytes: int,
        source_message_id: str | None,
        origin: str,
        persistence_policy: str,
        duration: float | None,
        dimensions: tuple[int, int] | None,
    ) -> "MediaRef":
        max_item_bytes = _validate_max_item_bytes(max_item_bytes)
        _raise_size_limit(len(data), max_item_bytes)

        normalized_mime = (
            normalize_media_mime_type(mime_type) if mime_type is not None else None
        )
        normalized_kind = _normalize_kind(kind)
        if normalized_kind is None:
            normalized_kind = (
                _kind_from_mime(normalized_mime)
                if normalized_mime is not None
                else MediaKind.FILE
            )

        if normalized_mime is None:
            detected_mime = _detect_mime(data, kind_hint=normalized_kind)
            if normalized_kind is MediaKind.FILE:
                normalized_mime = detected_mime or "application/octet-stream"
            elif detected_mime is None:
                raise MediaValidationError(
                    f"无法从 bytes 识别 {normalized_kind.value} 的 MIME 类型"
                )
            else:
                normalized_mime = detected_mime

        if normalized_kind is not MediaKind.FILE and _kind_from_mime(normalized_mime) is not normalized_kind:
            raise MediaValidationError(
                f"媒体 kind={normalized_kind.value!r} 与 MIME {normalized_mime!r} 不匹配"
            )

        _validate_magic(normalized_kind, normalized_mime, data)
        return cls(
            kind=normalized_kind,
            mime_type=normalized_mime,
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            data=data,
            source_message_id=source_message_id,
            origin=origin,
            persistence_policy=persistence_policy,
            duration=duration,
            dimensions=dimensions,
        )

    @classmethod
    def from_bytes(
        cls,
        data: bytes | bytearray | memoryview,
        *,
        kind: MediaKind | str | None = None,
        mime_type: str | None = None,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        source_message_id: str | None = None,
        origin: str = "inline",
        persistence_policy: str = "ephemeral",
        duration: float | None = None,
        dimensions: tuple[int, int] | None = None,
    ) -> "MediaRef":
        """Create a reference from bytes with strict size and signature checks."""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("MediaRef.from_bytes 需要 bytes-like 输入")
        max_item_bytes = _validate_max_item_bytes(max_item_bytes)
        source_size = data.nbytes if isinstance(data, memoryview) else len(data)
        _raise_size_limit(source_size, max_item_bytes)
        return cls._build(
            bytes(data),
            kind=kind,
            mime_type=mime_type,
            max_item_bytes=max_item_bytes,
            source_message_id=source_message_id,
            origin=origin,
            persistence_policy=persistence_policy,
            duration=duration,
            dimensions=dimensions,
        )

    @classmethod
    def from_base64(
        cls,
        value: str,
        *,
        kind: MediaKind | str | None = None,
        mime_type: str | None = None,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        source_message_id: str | None = None,
        origin: str = "base64",
        persistence_policy: str = "ephemeral",
        duration: float | None = None,
        dimensions: tuple[int, int] | None = None,
    ) -> "MediaRef":
        """Create a reference from strict standard base64.

        The compatibility prefixes ``base64|`` and ``base64://`` are accepted.
        Ordinary strings are never interpreted as local paths.
        """
        if not isinstance(value, str):
            raise TypeError("MediaRef.from_base64 需要字符串输入")
        max_item_bytes = _validate_max_item_bytes(max_item_bytes)
        if value.startswith("data:"):
            return cls.from_data_url(
                value,
                kind=kind,
                mime_type=mime_type,
                max_item_bytes=max_item_bytes,
                source_message_id=source_message_id,
                origin=origin,
                persistence_policy=persistence_policy,
                duration=duration,
                dimensions=dimensions,
            )
        if value.startswith("base64|"):
            value = value[len("base64|") :]
        elif value.startswith("base64://"):
            value = value[len("base64://") :]

        max_item_bytes = _validate_max_item_bytes(max_item_bytes)
        padding = len(value) - len(value.rstrip("="))
        decoded_upper_bound = (len(value) // 4) * 3 - min(padding, 2)
        if decoded_upper_bound > max_item_bytes:
            _raise_size_limit(decoded_upper_bound, max_item_bytes)

        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise MediaValidationError("非法 base64 媒体数据") from exc
        return cls.from_bytes(
            raw,
            kind=kind,
            mime_type=mime_type,
            max_item_bytes=max_item_bytes,
            source_message_id=source_message_id,
            origin=origin,
            persistence_policy=persistence_policy,
            duration=duration,
            dimensions=dimensions,
        )

    @classmethod
    def from_data_url(
        cls,
        value: str,
        *,
        kind: MediaKind | str | None = None,
        mime_type: str | None = None,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        source_message_id: str | None = None,
        origin: str = "data_url",
        persistence_policy: str = "ephemeral",
        duration: float | None = None,
        dimensions: tuple[int, int] | None = None,
    ) -> "MediaRef":
        """Create a reference from a ``data:<mime>;base64,<payload>`` URL."""
        if not isinstance(value, str):
            raise TypeError("MediaRef.from_data_url 需要字符串输入")
        match = _DATA_URL_RE.fullmatch(value)
        if match is None:
            raise MediaValidationError("无效的 data URL")
        metadata, payload = match.groups()
        metadata_parts = metadata.split(";")
        if len(metadata_parts) < 2 or metadata_parts[-1].lower() != "base64":
            raise MediaValidationError("data URL 必须使用 base64 编码")
        data_url_mime = normalize_media_mime_type(metadata_parts[0])
        if mime_type is not None and normalize_media_mime_type(mime_type) != data_url_mime:
            raise MediaValidationError("data URL MIME 与显式 mime_type 不一致")
        return cls.from_base64(
            payload,
            kind=kind,
            mime_type=data_url_mime,
            max_item_bytes=max_item_bytes,
            source_message_id=source_message_id,
            origin=origin,
            persistence_policy=persistence_policy,
            duration=duration,
            dimensions=dimensions,
        )

    @classmethod
    def from_managed_path(
        cls,
        path: str | PathLike[str],
        *,
        kind: MediaKind | str | None = None,
        mime_type: str | None = None,
        max_item_bytes: int = DEFAULT_MAX_ITEM_BYTES,
        source_message_id: str | None = None,
        origin: str = "managed_path",
        persistence_policy: str = "managed",
        duration: float | None = None,
        dimensions: tuple[int, int] | None = None,
    ) -> "MediaRef":
        """Read an explicitly managed local path into a validated reference."""
        if not isinstance(path, (str, PathLike)):
            raise TypeError("MediaRef.from_managed_path 需要路径输入")
        max_item_bytes = _validate_max_item_bytes(max_item_bytes)
        managed_path = Path(path)
        try:
            path_stat = managed_path.stat()
        except OSError as exc:
            raise MediaValidationError(
                f"媒体文件不存在或无法访问: {managed_path}"
            ) from exc
        if not stat_module.S_ISREG(path_stat.st_mode):
            raise MediaValidationError(f"媒体文件不是普通文件: {managed_path}")
        _raise_size_limit(path_stat.st_size, max_item_bytes)
        try:
            data = managed_path.read_bytes()
        except OSError as exc:
            raise MediaValidationError(f"无法读取媒体文件: {managed_path}") from exc
        return cls.from_bytes(
            data,
            kind=kind,
            mime_type=mime_type,
            max_item_bytes=max_item_bytes,
            source_message_id=source_message_id,
            origin=origin,
            persistence_policy=persistence_policy,
            duration=duration,
            dimensions=dimensions,
        )


class MediaPart:
    """Payload content wrapper around a validated :class:`MediaRef`.

    This is intentionally a small manual immutable class instead of a frozen
    ``slots=True`` dataclass.  A frozen slotted dataclass captures the temporary
    pre-slots class in its generated ``__setattr__`` closure; that breaks when
    this class is used as the first base of the ``File``/``Image`` facades.
    """

    __slots__ = ("media_ref",)

    def __init__(self, media_ref: MediaRef) -> None:
        if not isinstance(media_ref, MediaRef):
            raise TypeError("MediaPart.media_ref 必须是 MediaRef")
        if not media_ref.is_materialized:
            raise MediaValidationError("MediaPart 不能包装未物化的 MediaRef")
        object.__setattr__(self, "media_ref", media_ref)

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(mime_type={self.mime_type!r}, "
            f"size_bytes={self.size_bytes}, sha256={self.sha256!r})"
        )

    def __setattr__(self, name: str, value: Any) -> None:
        del value
        raise FrozenInstanceError(f"cannot assign to field {name!r}")

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError(f"cannot delete field {name!r}")

    def _equality_key(self) -> tuple[Any, ...]:
        ref = self.media_ref
        return (
            ref.kind,
            ref.mime_type,
            ref.size_bytes,
            ref.sha256,
            ref.data,
            ref.source_message_id,
            ref.duration,
            ref.dimensions,
        )

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return NotImplemented
        assert isinstance(other, MediaPart)
        return self._equality_key() == other._equality_key()

    def __hash__(self) -> int:
        return hash(self._equality_key())

    @property
    def kind(self) -> MediaKind:
        return self.media_ref.kind

    @property
    def mime_type(self) -> str:
        return self.media_ref.mime_type

    @property
    def size_bytes(self) -> int:
        return self.media_ref.size_bytes

    @property
    def sha256(self) -> str:
        return self.media_ref.sha256

    @property
    def value(self) -> str:
        """Legacy pure-base64 view of the validated bytes."""
        data = self.media_ref.data
        if data is None:
            raise MediaValidationError("未物化的 MediaRef 没有可编码的 data")
        return base64.b64encode(data).decode("ascii")

    @property
    def data_url(self) -> str:
        """Canonical data URL derived from validated MIME and bytes."""
        if self.media_ref.data is None:
            raise MediaValidationError("未物化的 MediaRef 无法生成 data URL")
        return f"data:{self.mime_type};base64,{self.value}"

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview, **kwargs: Any) -> "MediaPart":
        return cls(MediaRef.from_bytes(data, **kwargs))

    @classmethod
    def from_base64(cls, value: str, **kwargs: Any) -> "MediaPart":
        return cls(MediaRef.from_base64(value, **kwargs))

    @classmethod
    def from_data_url(cls, value: str, **kwargs: Any) -> "MediaPart":
        return cls(MediaRef.from_data_url(value, **kwargs))

    @classmethod
    def from_managed_path(cls, path: str | PathLike[str], **kwargs: Any) -> "MediaPart":
        return cls(MediaRef.from_managed_path(path, **kwargs))


__all__ = [
    "DEFAULT_MAX_ITEM_BYTES",
    "ABSOLUTE_MAX_ITEM_BYTES",
    "MediaKind",
    "MediaRef",
    "MediaPart",
    "normalize_media_mime_type",
    "redact_media_sources",
    "MediaValidationError",
    "MediaLimitError",
    "UnsupportedModalityError",
]
