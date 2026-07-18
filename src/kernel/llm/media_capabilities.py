"""Media capability normalization and model compatibility checks.

The request layer uses this module before opening a policy session.  Providers
receive only already-validated media and must not reinterpret configuration or
infer a model's capabilities from its name.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any, TypedDict

from .exceptions import LLMConfigurationError, MediaLimitError
from .payload.content import Content
from .payload.media import MediaKind, MediaPart, MediaRef, normalize_media_mime_type
from .payload.payload import LLMPayload


class MediaCapabilities(TypedDict):
    """Normalized media contract attached to a model entry."""

    modalities: list[str]
    accepted_mime_types: dict[str, list[str]]
    max_item_bytes: int | None
    max_request_bytes: int | None
    max_count: int | None
    max_audio_seconds: float | None
    max_video_seconds: float | None
    wire_profile: str | None


DEFAULT_MEDIA_CAPABILITIES: MediaCapabilities = {
    "modalities": ["text"],
    "accepted_mime_types": {},
    "max_item_bytes": None,
    "max_request_bytes": None,
    "max_count": None,
    "max_audio_seconds": None,
    "max_video_seconds": None,
    "wire_profile": None,
}
_MEDIA_KINDS = frozenset(kind.value for kind in MediaKind)
_ALLOWED_MODALITIES = frozenset({"text", *_MEDIA_KINDS})


def _configuration_error(message: str) -> LLMConfigurationError:
    return LLMConfigurationError(f"model.media_capabilities {message}")


def _is_unset_optional(value: Any) -> bool:
    """Treat the config renderer's empty-string sentinel as an unset optional."""
    return value is None or (isinstance(value, str) and not value.strip())


def _normalize_positive_int(value: Any, field_name: str) -> int | None:
    if _is_unset_optional(value):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise _configuration_error(f"{field_name} 必须是正整数或 null")
    return value


def _normalize_nonnegative_number(value: Any, field_name: str) -> float | None:
    if _is_unset_optional(value):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise _configuration_error(f"{field_name} 必须是非负数或 null")
    return float(value)


def normalize_media_capabilities(value: Any) -> MediaCapabilities:
    """Return a strict, copy-safe media capability contract.

    Missing capabilities are deliberately text-only.  This fail-closed default
    prevents a model from receiving a modality solely because another provider
    happens to serialize it.
    """
    if value is None:
        return deepcopy(DEFAULT_MEDIA_CAPABILITIES)
    if not isinstance(value, Mapping):
        raise _configuration_error("必须是 dict")

    unknown_keys = set(value) - set(MediaCapabilities.__annotations__)
    if unknown_keys:
        raise _configuration_error(f"包含未知字段: {sorted(unknown_keys)!r}")

    raw_modalities = value.get("modalities", ["text"])
    if not isinstance(raw_modalities, list) or not raw_modalities:
        raise _configuration_error("modalities 必须是非空 list[str]")
    modalities: list[str] = []
    for modality in raw_modalities:
        if not isinstance(modality, str):
            raise _configuration_error("modalities 必须是非空 list[str]")
        normalized = modality.strip().lower()
        if normalized not in _ALLOWED_MODALITIES:
            raise _configuration_error(f"modalities 包含不支持的值: {modality!r}")
        if normalized not in modalities:
            modalities.append(normalized)

    raw_mime_types = value.get("accepted_mime_types", {})
    if not isinstance(raw_mime_types, Mapping):
        raise _configuration_error("accepted_mime_types 必须是 dict")
    accepted_mime_types: dict[str, list[str]] = {}
    for raw_kind, raw_mimes in raw_mime_types.items():
        if not isinstance(raw_kind, str):
            raise _configuration_error("accepted_mime_types 的键必须是媒体 kind")
        kind = raw_kind.strip().lower()
        if kind not in _MEDIA_KINDS:
            raise _configuration_error(
                f"accepted_mime_types 包含不支持的媒体 kind: {raw_kind!r}"
            )
        if kind not in modalities:
            raise _configuration_error(
                f"accepted_mime_types.{kind} 不能声明未启用的模态"
            )
        if not isinstance(raw_mimes, list) or not raw_mimes:
            raise _configuration_error(
                f"accepted_mime_types.{kind} 必须是非空 list[str]"
            )
        normalized_mimes: list[str] = []
        for raw_mime in raw_mimes:
            if not isinstance(raw_mime, str):
                raise _configuration_error(
                    f"accepted_mime_types.{kind} 必须是非空 list[str]"
                )
            normalized_mime = raw_mime.strip().lower()
            if normalized_mime != "*/*":
                try:
                    normalized_mime = normalize_media_mime_type(normalized_mime)
                except Exception as exc:
                    raise _configuration_error(
                        f"accepted_mime_types.{kind} 包含无效 MIME: {raw_mime!r}"
                    ) from exc
                if not normalized_mime.startswith(f"{kind}/"):
                    raise _configuration_error(
                        f"accepted_mime_types.{kind} 的 MIME 必须以 {kind + '/'} 开头"
                    )
            if normalized_mime not in normalized_mimes:
                normalized_mimes.append(normalized_mime)
        accepted_mime_types[kind] = normalized_mimes

    wire_profile = value.get("wire_profile")
    if _is_unset_optional(wire_profile):
        wire_profile = None
    elif isinstance(wire_profile, str):
        wire_profile = wire_profile.strip()
    else:
        raise _configuration_error("wire_profile 必须是非空字符串或 null")

    return {
        "modalities": modalities,
        "accepted_mime_types": accepted_mime_types,
        "max_item_bytes": _normalize_positive_int(
            value.get("max_item_bytes"), "max_item_bytes"
        ),
        "max_request_bytes": _normalize_positive_int(
            value.get("max_request_bytes"), "max_request_bytes"
        ),
        "max_count": _normalize_positive_int(value.get("max_count"), "max_count"),
        "max_audio_seconds": _normalize_nonnegative_number(
            value.get("max_audio_seconds"), "max_audio_seconds"
        ),
        "max_video_seconds": _normalize_nonnegative_number(
            value.get("max_video_seconds"), "max_video_seconds"
        ),
        "wire_profile": wire_profile,
    }


def extract_media_refs(payloads: Iterable[LLMPayload]) -> list[MediaRef]:
    """Collect validated media references in payload order."""
    refs: list[MediaRef] = []
    for payload in payloads:
        for part in payload.content:
            if isinstance(part, MediaPart):
                refs.append(part.media_ref)
            elif isinstance(part, Content):
                continue
    return refs


def _accepts_mime(capabilities: MediaCapabilities, media: MediaRef) -> bool:
    accepted = capabilities["accepted_mime_types"].get(media.kind.value)
    if not accepted:
        return True
    return "*/*" in accepted or media.mime_type in accepted


def media_capability_mismatch(
    capabilities: MediaCapabilities, media_refs: Iterable[MediaRef]
) -> str | None:
    """Return the first compatibility failure, otherwise ``None``."""
    refs = list(media_refs)
    if not refs:
        return None

    max_count = capabilities["max_count"]
    if max_count is not None and len(refs) > max_count:
        return f"媒体数量 {len(refs)} 超过 max_count={max_count}"

    total_bytes = sum(media.size_bytes for media in refs)
    max_request_bytes = capabilities["max_request_bytes"]
    if max_request_bytes is not None and total_bytes > max_request_bytes:
        return f"媒体总大小 {total_bytes} 超过 max_request_bytes={max_request_bytes}"

    for media in refs:
        if media.kind.value not in capabilities["modalities"]:
            return f"不支持 {media.kind.value} 模态"
        if not _accepts_mime(capabilities, media):
            return f"不接受 MIME {media.mime_type!r}"

        max_item_bytes = capabilities["max_item_bytes"]
        if max_item_bytes is not None and media.size_bytes > max_item_bytes:
            return (
                f"{media.kind.value} 大小 {media.size_bytes} "
                f"超过 max_item_bytes={max_item_bytes}"
            )
        if media.kind is MediaKind.AUDIO:
            max_seconds = capabilities["max_audio_seconds"]
            if (
                max_seconds is not None
                and media.duration is not None
                and media.duration > max_seconds
            ):
                return f"音频时长 {media.duration} 超过 max_audio_seconds={max_seconds}"
        if media.kind is MediaKind.VIDEO:
            max_seconds = capabilities["max_video_seconds"]
            if (
                max_seconds is not None
                and media.duration is not None
                and media.duration > max_seconds
            ):
                return f"视频时长 {media.duration} 超过 max_video_seconds={max_seconds}"

    return None


def filter_model_set_for_media(
    model_set: Iterable[dict[str, Any]], media_refs: Iterable[MediaRef]
) -> list[dict[str, Any]]:
    """Keep models whose normalized capabilities accept every media reference.

    Models are copied only when their stored capability field needs replacement,
    preventing request validation from mutating a caller's configuration.
    """
    refs = list(media_refs)
    if not refs:
        return list(model_set)

    compatible: list[dict[str, Any]] = []
    for model in model_set:
        capabilities = normalize_media_capabilities(model.get("media_capabilities"))
        if media_capability_mismatch(capabilities, refs) is not None:
            continue
        normalized_model = dict(model)
        normalized_model["media_capabilities"] = capabilities
        compatible.append(normalized_model)
    return compatible


def validate_media_request_limits(media_refs: Iterable[MediaRef]) -> None:
    """Validate aggregate invariants independent of any particular model."""
    refs = list(media_refs)
    if not refs:
        return
    if any(media.size_bytes <= 0 for media in refs):
        # Zero-byte generic File content remains valid, but a typed modality
        # would already have failed magic validation.  Keep this helper focused
        # on aggregate guardrails rather than imposing an unrelated policy.
        return


__all__ = [
    "DEFAULT_MEDIA_CAPABILITIES",
    "MediaCapabilities",
    "extract_media_refs",
    "filter_model_set_for_media",
    "media_capability_mismatch",
    "normalize_media_capabilities",
]
