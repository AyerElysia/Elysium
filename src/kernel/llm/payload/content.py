"""LLM payload content types.

Media facades retain the historical ``Content``/``File`` API while delegating all
binary decoding and validation to :mod:`.media`.  A plain string is encoded media
(base64, ``base64|`` or a data URL), never an implicit filesystem path.  Use a
``Path`` instance or ``from_managed_path`` for an explicitly managed file.
"""

from __future__ import annotations

from dataclasses import dataclass
from os import PathLike
from typing import Any, BinaryIO, Self

from .media import MediaKind, MediaPart, MediaRef, MediaValidationError


@dataclass(frozen=True, slots=True)
class Content:
    """Payload content base class."""


@dataclass(frozen=True, slots=True)
class Text(Content):
    """Text content."""

    text: str


@dataclass(frozen=True, slots=True)
class ReasoningText(Content):
    """Thinking/reasoning content returned by a provider."""

    text: str
    signature: str | None = None
    redacted_data: str | None = None


class File(MediaPart, Content):
    """Arbitrary binary content backed by a validated :class:`MediaRef`.

    ``str`` inputs must be strict base64 or a base64 data URL.  A ``PathLike``
    input deliberately opts in to reading an existing managed local file.
    """

    __slots__ = ()
    _media_kind = MediaKind.FILE
    _default_mime_type: str | None = None

    def __init__(
        self,
        source: str | PathLike[str] | BinaryIO | bytes | bytearray | memoryview,
        *,
        mime_type: str | None = None,
        max_item_bytes: int = MediaRef.DEFAULT_MAX_ITEM_BYTES,
        source_message_id: str | None = None,
        persistence_policy: str | None = None,
        duration: float | None = None,
        dimensions: tuple[int, int] | None = None,
    ) -> None:
        ref = self._build_ref(
            source,
            mime_type=mime_type,
            max_item_bytes=max_item_bytes,
            source_message_id=source_message_id,
            persistence_policy=persistence_policy,
            duration=duration,
            dimensions=dimensions,
        )
        object.__setattr__(self, "media_ref", ref)

    @classmethod
    def _effective_mime_type(cls, mime_type: str | None) -> str | None:
        return mime_type if mime_type is not None else cls._default_mime_type

    @classmethod
    def _build_ref(
        cls,
        source: str | PathLike[str] | BinaryIO | bytes | bytearray | memoryview,
        *,
        mime_type: str | None,
        max_item_bytes: int,
        source_message_id: str | None,
        persistence_policy: str | None,
        duration: float | None,
        dimensions: tuple[int, int] | None,
    ) -> MediaRef:
        kwargs: dict[str, Any] = {
            "kind": cls._media_kind,
            "mime_type": cls._effective_mime_type(mime_type),
            "max_item_bytes": max_item_bytes,
            "source_message_id": source_message_id,
            "duration": duration,
            "dimensions": dimensions,
        }
        if persistence_policy is not None:
            kwargs["persistence_policy"] = persistence_policy

        if isinstance(source, (bytes, bytearray, memoryview)):
            return MediaRef.from_bytes(source, origin="inline", **kwargs)
        if isinstance(source, PathLike):
            return MediaRef.from_managed_path(source, **kwargs)
        if isinstance(source, str):
            if source.startswith("data:"):
                return MediaRef.from_data_url(source, **kwargs)
            return MediaRef.from_base64(source, **kwargs)
        if hasattr(source, "read"):
            raw = source.read()
            if not isinstance(raw, (bytes, bytearray, memoryview)):
                raise TypeError("File 文件对象的 read() 必须返回 bytes-like 数据")
            return MediaRef.from_bytes(raw, origin="stream", **kwargs)
        raise TypeError(
            f"File 不支持的输入类型：{type(source).__name__}。"
            "请传入 base64/data URL、PathLike、bytes-like 或二进制文件对象。"
        )

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview, **kwargs: Any) -> Self:
        return cls._from_ref(MediaRef.from_bytes(data, kind=cls._media_kind, **kwargs))

    @classmethod
    def from_base64(cls, value: str, **kwargs: Any) -> Self:
        return cls._from_ref(MediaRef.from_base64(value, kind=cls._media_kind, **kwargs))

    @classmethod
    def from_data_url(cls, value: str, **kwargs: Any) -> Self:
        return cls._from_ref(MediaRef.from_data_url(value, kind=cls._media_kind, **kwargs))

    @classmethod
    def from_managed_path(cls, path: str | PathLike[str], **kwargs: Any) -> Self:
        return cls._from_ref(
            MediaRef.from_managed_path(path, kind=cls._media_kind, **kwargs)
        )

    @classmethod
    def _from_ref(cls, ref: MediaRef) -> Self:
        if ref.kind is not cls._media_kind:
            raise ValueError(
                f"{cls.__name__} 不能包装 kind={ref.kind.value!r} 的 MediaRef"
            )
        if not ref.is_materialized:
            raise MediaValidationError(
                f"{cls.__name__} 不能包装未物化的 MediaRef"
            )
        instance = cls.__new__(cls)
        object.__setattr__(instance, "media_ref", ref)
        return instance

    def __repr__(self) -> str:
        return super().__repr__()


class Image(File):
    """Validated image content.

    The true MIME type is inferred from an image signature for bytes/base64
    inputs, or taken from a matching data URL.  ``Image("/path/file.png")`` is
    intentionally invalid; use ``Image(Path(...))`` or ``Image.from_managed_path``.
    """

    __slots__ = ()
    _media_kind = MediaKind.IMAGE


class Audio(File):
    """Validated audio content with a true MIME type."""

    __slots__ = ()
    _media_kind = MediaKind.AUDIO


class Video(File):
    """Validated video content with a true MIME type."""

    __slots__ = ()
    _media_kind = MediaKind.VIDEO


__all__ = [
    "Content",
    "Text",
    "ReasoningText",
    "File",
    "Image",
    "Audio",
    "Video",
]
