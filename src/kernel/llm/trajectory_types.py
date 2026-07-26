"""Stable, text-only trajectory record helpers.

The trajectory schema intentionally uses a mapping instead of a frozen dataclass.
New producers can add fields through ``metadata`` or ``extensions`` without
making old readers unable to consume a record.
"""

from __future__ import annotations

import base64
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from enum import Enum
from os import PathLike
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from .payload.media import MediaPart, MediaRef, redact_media_sources

TRAJECTORY_SCHEMA_VERSION = 1
REDACTED_VALUE = "[removed]"

# This is a contract for the first schema version, not a closed list of fields.
TRAJECTORY_FIELDS = frozenset(
    {
        "schema_version",
        "trace_id",
        "attempt_id",
        "request_id",
        "parent_attempt_id",
        "timestamp",
        "request_name",
        "task_name",
        "task_tags",
        "stream_id",
        "heartbeat_run_id",
        "call_id",
        "model",
        "model_identifier",
        "api_provider",
        "policy_meta",
        "messages",
        "response",
        "tool_results",
        "usage",
        "latency_s",
        "success",
        "error",
        "error_type",
        "metadata",
        "extensions",
    }
)

_SOURCE_KEYS = frozenset(
    {
        "base64",
        "imagebase64",
        "audiobase64",
        "voicebase64",
        "videobase64",
        "raw",
        "rawdata",
        "path",
        "filepath",
        "localpath",
        "temppath",
        "file",
        "url",
        "uri",
        "src",
        "fileurl",
        "downloadurl",
        "source",
    }
)
_MEDIA_CONTEXT_KEYS = frozenset(
    {
        "media",
        "attachments",
        "imageurl",
        "inputimage",
        "inputaudio",
        "outputaudio",
        "videourl",
        "inputvideo",
    }
)
_MEDIA_TYPE_VALUES = frozenset(
    {
        "image",
        "imageurl",
        "inputimage",
        "audio",
        "inputaudio",
        "outputaudio",
        "video",
        "videourl",
        "inputvideo",
        "file",
        "emoji",
        "voice",
    }
)
_MEDIA_BODY_KEYS = frozenset({"data", "bytes", "rawdata"})
_SAFE_MEDIA_METADATA_KEYS = frozenset(
    {
        "kind",
        "mimetype",
        "mime_type",
        "sizebytes",
        "size_bytes",
        "sha256",
        "sourcemessageid",
        "source_message_id",
        "origin",
        "persistencepolicy",
        "persistence_policy",
        "duration",
        "dimensions",
    }
)
_DATA_URL_RE = re.compile(r"^data:[^,]+,", re.IGNORECASE | re.DOTALL)
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
# Blobs interpolated into longer text are scrubbed in place rather than
# dropping the whole string, so surrounding prompt text stays usable.
_EMBEDDED_SCAN_MIN_LEN = 256
# Deliberately excludes whitespace: allowing it would let the match run past
# the blob and swallow the surrounding prose.
_EMBEDDED_DATA_URL_RE = re.compile(
    r"data:[\w.+-]+/[\w.+-]+;base64,[A-Za-z0-9+/=]{64,}",
    re.IGNORECASE,
)
_EMBEDDED_BASE64_RUN_RE = re.compile(r"[A-Za-z0-9+/]{256,}={0,2}")
_MEDIA_URL_RE = re.compile(
    r"^(?:https?|file)://[^\s]+\.(?:avif|bmp|gif|jpe?g|m4a|mp3|mp4|mpeg|ogg|pdf|png|svg|wav|webm|webp|zip)(?:[?#].*)?$",
    re.IGNORECASE,
)
_MEDIA_PATH_RE = re.compile(
    r"^(?:~[\\/]|\.\.?[\\/]|[\\/]|[A-Za-z]:[\\/]|file://).+",
    re.IGNORECASE,
)


class TrajectoryRecord(TypedDict, total=False):
    """Typed view of the stable v1 fields.

    ``total=False`` is deliberate: readers must tolerate records produced by a
    newer writer, while ``ensure_trajectory_record`` supplies the v1 defaults.
    """

    schema_version: int
    trace_id: str | None
    attempt_id: str | None
    request_id: str | None
    parent_attempt_id: str | None
    timestamp: str
    request_name: str
    task_name: str
    task_tags: list[str]
    stream_id: str | None
    heartbeat_run_id: str | None
    call_id: str | None
    model: str | None
    model_identifier: str | None
    api_provider: str | None
    policy_meta: dict[str, Any]
    messages: list[dict[str, Any]]
    response: Any
    tool_results: list[dict[str, Any]]
    usage: dict[str, Any]
    latency_s: float | None
    success: bool | None
    error: str | None
    error_type: str | None
    metadata: dict[str, Any]
    extensions: dict[str, Any]
    # Future producers may add fields without changing this type's defaults.
    extra: NotRequired[Any]


def utc_timestamp(value: datetime | None = None) -> str:
    """Return an ISO-8601 UTC timestamp with a stable ``Z`` suffix."""
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    return current.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_trajectory_id(prefix: str = "") -> str:
    """Create a collision-resistant identifier for a request or attempt."""
    value = uuid.uuid4().hex
    return f"{prefix}_{value}" if prefix else value


def derive_task_tags(request_name: str | None) -> list[str]:
    """Derive small, deterministic category tags from a request name."""
    normalized = str(request_name or "").strip().lower()
    if not normalized:
        return ["llm"]
    parts = [part for part in re.split(r"[.:/_\\-]+", normalized) if part]
    tags: list[str] = ["llm"]
    for part in parts[:4]:
        if part not in tags:
            tags.append(part)
    return tags


def _media_descriptor(value: MediaRef | MediaPart) -> dict[str, Any]:
    ref = value if isinstance(value, MediaRef) else value.media_ref
    kind = getattr(ref.kind, "value", ref.kind)
    return {
        "kind": str(kind),
        "mime_type": ref.mime_type,
        "size_bytes": ref.size_bytes,
        "sha256": ref.sha256,
        "source_message_id": ref.source_message_id,
        "origin": ref.origin,
        "persistence_policy": ref.persistence_policy,
        "duration": ref.duration,
        "dimensions": list(ref.dimensions) if ref.dimensions is not None else None,
    }


def _normalized_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "").replace("-", "")


def _looks_like_long_base64(value: str) -> bool:
    compact = "".join(value.split())
    if len(compact) < 256 or len(compact) % 4 != 0:
        return False
    return bool(_BASE64_RE.fullmatch(compact))


def _scrub_embedded_blobs(value: str) -> str:
    """Replace media blobs embedded inside otherwise-normal text.

    Whole-value checks miss the common case where a caller interpolates a data
    URL or a raw base64 body into a longer prompt, so the payload has to be
    scrubbed in place instead of rejecting the whole string.
    """
    if len(value) < _EMBEDDED_SCAN_MIN_LEN:
        return value
    scrubbed = _EMBEDDED_DATA_URL_RE.sub(REDACTED_VALUE, value)
    scrubbed = _EMBEDDED_BASE64_RUN_RE.sub(REDACTED_VALUE, scrubbed)
    return scrubbed


def _sanitize_string(
    value: str,
    *,
    parent_key: str | None,
    source_value: bool,
) -> str:
    if source_value or _DATA_URL_RE.match(value):
        return REDACTED_VALUE
    if value.startswith(("base64:", "base64|", "base64://")):
        return REDACTED_VALUE
    if _looks_like_long_base64(value):
        return REDACTED_VALUE

    normalized_parent = _normalized_key(parent_key)
    if normalized_parent in _SOURCE_KEYS and normalized_parent not in _SAFE_MEDIA_METADATA_KEYS:
        return REDACTED_VALUE
    if _MEDIA_URL_RE.fullmatch(value.strip()) or _MEDIA_PATH_RE.fullmatch(value.strip()):
        return REDACTED_VALUE
    return _scrub_embedded_blobs(value)


def sanitize_text_only(
    value: Any,
    *,
    media_context: bool = False,
    source_value: bool = False,
    _parent_key: str | None = None,
) -> Any:
    """Return a JSON-safe copy with media bodies and sources removed.

    ``redact_media_sources`` is the first pass so legacy and provider-shaped
    media mappings follow the same redaction rules as the rest of the LLM
    stack.  The second pass handles media objects, paths, data URLs, and
    non-JSON Python values that the shared helper cannot inspect.
    """
    if isinstance(value, (MediaRef, MediaPart)):
        return _media_descriptor(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return REDACTED_VALUE
    if isinstance(value, PathLike) and not isinstance(value, str):
        return REDACTED_VALUE
    if isinstance(value, Enum):
        return sanitize_text_only(
            value.value,
            media_context=media_context,
            source_value=source_value,
            _parent_key=_parent_key,
        )
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if value is None or isinstance(value, (bool, int, str)):
        if isinstance(value, str):
            return _sanitize_string(
                value,
                parent_key=_parent_key,
                source_value=source_value,
            )
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None

    if isinstance(value, Mapping):
        # Reuse the existing media redactor before applying stricter generic
        # key handling for paths and transport source fields.
        try:
            redacted = redact_media_sources(
                value,
                media_context=media_context,
                source_value=source_value,
            )
        except Exception:
            redacted = value

        sanitized: dict[str, Any] = {}
        mapping_media_context = media_context or any(
            _normalized_key(key) in _MEDIA_CONTEXT_KEYS
            or (
                _normalized_key(key) == "type"
                and _normalized_key(child) in _MEDIA_TYPE_VALUES
            )
            for key, child in redacted.items()
        )
        for key, child in redacted.items():
            key_text = str(key)
            normalized = _normalized_key(key_text)
            child_source = normalized in _SOURCE_KEYS
            if child_source and normalized not in _SAFE_MEDIA_METADATA_KEYS:
                sanitized[key_text] = REDACTED_VALUE
                continue
            # Generic result dictionaries may legitimately use a `data` field;
            # only treat it as a media body after a media context was detected.
            if normalized in _MEDIA_BODY_KEYS and mapping_media_context:
                sanitized[key_text] = REDACTED_VALUE
                continue
            sanitized[key_text] = sanitize_text_only(
                child,
                media_context=mapping_media_context,
                source_value=child_source and normalized not in _SAFE_MEDIA_METADATA_KEYS,
                _parent_key=key_text,
            )
        return sanitized

    if isinstance(value, (list, tuple)):
        return [
            sanitize_text_only(
                item,
                media_context=media_context,
                source_value=source_value,
                _parent_key=_parent_key,
            )
            for item in value
        ]
    if isinstance(value, (set, frozenset)):
        return [
            sanitize_text_only(
                item,
                media_context=media_context,
                source_value=source_value,
                _parent_key=_parent_key,
            )
            for item in sorted(value, key=repr)
        ]

    if is_dataclass(value):
        try:
            return sanitize_text_only(asdict(value), media_context=media_context)
        except Exception:
            return _sanitize_string(str(value), parent_key=_parent_key, source_value=source_value)

    to_descriptor = getattr(value, "to_descriptor", None)
    if callable(to_descriptor):
        try:
            return sanitize_text_only(to_descriptor(), media_context=True)
        except Exception:
            pass

    # Unknown objects are represented as text, then passed through the same
    # source checks.  This keeps JSON encoding deterministic without persisting
    # object internals that may contain binary data.
    return _sanitize_string(str(value), parent_key=_parent_key, source_value=source_value)


def ensure_trajectory_record(record: Mapping[str, Any] | None = None) -> TrajectoryRecord:
    """Fill v1 defaults while preserving unknown extension fields."""
    source = dict(record or {})
    normalized = sanitize_text_only(source)
    if not isinstance(normalized, dict):
        normalized = {}

    defaults: dict[str, Any] = {
        "schema_version": TRAJECTORY_SCHEMA_VERSION,
        "trace_id": None,
        "attempt_id": None,
        "request_id": None,
        "parent_attempt_id": None,
        "timestamp": utc_timestamp(),
        "request_name": "",
        "task_name": "",
        "task_tags": [],
        "stream_id": None,
        "heartbeat_run_id": None,
        "call_id": None,
        "model": None,
        "model_identifier": None,
        "api_provider": None,
        "policy_meta": {},
        "messages": [],
        "response": None,
        "tool_results": [],
        "usage": {},
        "latency_s": None,
        "success": None,
        "error": None,
        "error_type": None,
        "metadata": {},
        "extensions": {},
    }
    defaults.update(normalized)
    defaults["schema_version"] = TRAJECTORY_SCHEMA_VERSION

    # Producers other than LLMRequest may omit the task fields; derive them here
    # so every row in the lake stays queryable by task without a second pass.
    if not defaults.get("task_name") and defaults.get("request_name"):
        defaults["task_name"] = defaults["request_name"]
    if not isinstance(defaults.get("task_tags"), list) or not defaults["task_tags"]:
        defaults["task_tags"] = derive_task_tags(
            defaults.get("task_name") or defaults.get("request_name")
        )
    if not isinstance(defaults.get("messages"), list):
        defaults["messages"] = []
    if not isinstance(defaults.get("tool_results"), list):
        defaults["tool_results"] = []
    for key in ("policy_meta", "usage", "metadata", "extensions"):
        if not isinstance(defaults.get(key), dict):
            defaults[key] = {}
    return defaults  # type: ignore[return-value]


def new_trajectory_record(**values: Any) -> TrajectoryRecord:
    """Construct a v1 record with stable defaults and caller-supplied values."""
    return ensure_trajectory_record(values)


__all__ = [
    "TRAJECTORY_SCHEMA_VERSION",
    "TRAJECTORY_FIELDS",
    "REDACTED_VALUE",
    "TrajectoryRecord",
    "utc_timestamp",
    "new_trajectory_id",
    "derive_task_tags",
    "sanitize_text_only",
    "ensure_trajectory_record",
    "new_trajectory_record",
]
