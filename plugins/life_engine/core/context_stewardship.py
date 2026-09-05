"""Subject-authored continuity checkpoints for bounded LLM context.

The module deliberately separates three things that the legacy compactor
mixed together:

* context pressure is a content-neutral transport fact;
* continuity text is authored only by the active consciousness instance;
* an emergency recovery projection may hide groups for one maintenance attempt,
  but never mutates or replaces the full rolling context.

No helper in this module decides which experience matters or rewrites subject
meaning.  Exact released prompt groups are archived by content hash before a
checkpoint is installed, while authoritative Life Events and trajectories stay
untouched.  When the full window no longer fits, a bounded maintenance attempt
may expose only content-neutral group references so the active subject can read
exact groups and author a checkpoint.  That attempt is not a normal response
path and the caller's full payload list remains unchanged.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, ClassVar

from src.app.plugin_system.base import BaseAction, BaseTool
from src.kernel.llm import (
    ROLE,
    Audio,
    File,
    Image,
    LLMPayload,
    ReasoningText,
    Text,
    ToolCall,
    ToolResult,
    Video,
)
from src.kernel.llm.exceptions import LLMContextError
from src.kernel.llm.token_counter import count_payload_tokens
from src.kernel.storage import canonical_json, canonical_json_sha256

LEGACY_SUMMARY_OPEN = "<compressed_life_chatter_context>"
LEGACY_SUMMARY_CLOSE = "</compressed_life_chatter_context>"
LEGACY_SUMMARY_INTRO = (
    "以下是因上下文窗口限制而压缩的旧 life_chatter 对话片段；"
    "请把它视为此前已经发生的背景，不要当作新的用户消息："
)

PRESSURE_OPEN = "<context_pressure_notice>"
PRESSURE_CLOSE = "</context_pressure_notice>"
COMPRESSION_REQUIRED_OPEN = "<context_compression_required>"
COMPRESSION_REQUIRED_CLOSE = "</context_compression_required>"
CHECKPOINT_OPEN = "<subject_self_continuity_checkpoint>"
CHECKPOINT_CLOSE = "</subject_self_continuity_checkpoint>"
OMISSION_OPEN = "<mechanical_context_omission>"
OMISSION_CLOSE = "</mechanical_context_omission>"

GROUP_SCHEMA = "elysium.context_group.v1"
MANIFEST_SCHEMA = "elysium.context_group_manifest.v1"
PRESSURE_SCHEMA = "elysium.context_pressure_notice.v1"
COMPRESSION_REQUIRED_SCHEMA = "elysium.context_compression_required.v1"
CHECKPOINT_SCHEMA = "elysium.subject_self_continuity_checkpoint.v1"
OMISSION_SCHEMA = "elysium.mechanical_context_omission.v1"
ARCHIVE_SCHEMA = "elysium.context_group_archive.v1"
ARCHIVE_NAMESPACE = "life_chatter.context_archive"
HEARTBEAT_ARCHIVE_NAMESPACE = "life_heartbeat.context_archive"
CHATTER_RUNTIME_KEY = "life_chatter"
HEARTBEAT_RUNTIME_KEY = "life_heartbeat"
CHATTER_ARCHIVE_SUBDIR = "context_archive"
HEARTBEAT_ARCHIVE_SUBDIR = "heartbeat_context_archive"
ARCHIVE_MAX_BYTES = 12 * 1024 * 1024
DEFAULT_PRESSURE_RATIO = 0.75
DEFAULT_PRESSURE_MAX_GROUPS = 24
DEFAULT_CHECKPOINT_MAX_BYTES = 32 * 1024
DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES = 8 * 1024
DEFAULT_ARCHIVE_READ_BYTES = 8 * 1024
MAX_ARCHIVE_READ_BYTES = 16 * 1024


class ContextStewardshipError(ValueError):
    """A subject checkpoint or archive operation failed closed."""


class ContextGroupArchiveNotFound(ContextStewardshipError):
    """An immutable group is not persisted yet and may still be live."""


@dataclass(frozen=True, slots=True)
class ContextGroupRecord:
    """One exact, content-addressed prompt group and content-neutral metadata."""

    ordinal: int
    group_ref: str
    utf8_bytes: int
    payload_count: int
    open_tool_chain: bool
    legacy_transport_projection: bool
    record: dict[str, Any]

    def public_descriptor(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "group_ref": self.group_ref,
            "utf8_bytes": self.utf8_bytes,
            "payload_count": self.payload_count,
            "open_tool_chain": self.open_tool_chain,
            "legacy_transport_projection": self.legacy_transport_projection,
        }


@dataclass(frozen=True, slots=True)
class ContextGroupManifest:
    """Stable manifest of groups that can be released at the current boundary."""

    source_manifest_sha256: str
    current_checkpoint_revision: int
    groups: tuple[ContextGroupRecord, ...]


@dataclass(frozen=True, slots=True)
class SubjectCheckpointCommand:
    """The active subject's exact command; semantic fields remain open text."""

    actor_consciousness_instance_id: str
    thought: str
    continuity_text: str
    source_manifest_sha256: str
    expected_revision: int
    release_through_group_ref: str
    retain_exact_group_refs: tuple[str, ...]

    @property
    def command_sha256(self) -> str:
        return canonical_json_sha256(
            {
                "actor_consciousness_instance_id": self.actor_consciousness_instance_id,
                "thought": self.thought,
                "continuity_text": self.continuity_text,
                "source_manifest_sha256": self.source_manifest_sha256,
                "expected_revision": self.expected_revision,
                "release_through_group_ref": self.release_through_group_ref,
                "retain_exact_group_refs": list(self.retain_exact_group_refs),
            }
        )


@dataclass(frozen=True, slots=True)
class PreparedSubjectCheckpoint:
    """A fully validated projection mutation, not yet installed."""

    command: SubjectCheckpointCommand
    checkpoint_id: str
    revision: int
    checkpoint_payload: LLMPayload
    payloads: list[LLMPayload]
    released_groups: tuple[ContextGroupRecord, ...]
    retained_groups: tuple[ContextGroupRecord, ...]
    before_utf8_bytes: int
    after_utf8_bytes: int


@dataclass(frozen=True, slots=True)
class ContextStewardshipResult:
    """Content-neutral result returned to the Chatter safe boundary."""

    triggered: bool
    payloads: list[LLMPayload]
    before_utf8_bytes: int
    after_utf8_bytes: int
    released_groups: int = 0
    checkpoint_id: str = ""
    revision: int = 0
    stop_reason: str = ""


_PENDING_LOCK = threading.Lock()
_PENDING_CHECKPOINTS: dict[str, SubjectCheckpointCommand] = {}
_TRANSIENT_PRESSURE_PARTS: dict[int, Text] = {}
_LIVE_WINDOWS: dict[str, "LiveContextWindow"] = {}
_RECOVERY_MARKER_ATTR = "_elysium_subject_context_recovery_used"


@dataclass(frozen=True, slots=True)
class LiveContextWindow:
    """One surface's current private rolling payloads and archive identity."""

    runtime_key: str
    payloads: list[LLMPayload]
    archive_namespace: str
    local_archive_subdir: str
    workspace_path: str = ""


def _pending_key(actor_consciousness_instance_id: str, runtime_key: str) -> str:
    actor = str(actor_consciousness_instance_id or "").strip()
    runtime = str(runtime_key or CHATTER_RUNTIME_KEY).strip() or CHATTER_RUNTIME_KEY
    return f"{runtime}:{actor}"


def register_live_context(window: LiveContextWindow) -> None:
    runtime = str(window.runtime_key or "").strip()
    if not runtime:
        raise ContextStewardshipError("live context runtime_key is missing")
    with _PENDING_LOCK:
        _LIVE_WINDOWS[runtime] = window


def unregister_live_context(runtime_key: str) -> None:
    with _PENDING_LOCK:
        _LIVE_WINDOWS.pop(str(runtime_key or "").strip(), None)


def get_live_context(runtime_key: str) -> LiveContextWindow | None:
    with _PENDING_LOCK:
        return _LIVE_WINDOWS.get(str(runtime_key or "").strip())


def archive_target_for_runtime(runtime_key: str) -> tuple[str, str]:
    runtime = str(runtime_key or CHATTER_RUNTIME_KEY).strip() or CHATTER_RUNTIME_KEY
    if runtime == HEARTBEAT_RUNTIME_KEY:
        return HEARTBEAT_ARCHIVE_NAMESPACE, HEARTBEAT_ARCHIVE_SUBDIR
    return ARCHIVE_NAMESPACE, CHATTER_ARCHIVE_SUBDIR


def _json_safe(value: Any) -> Any:
    """Return a deterministic JSON value without retaining object identity."""

    return json.loads(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _part_record(part: object) -> dict[str, Any]:
    if isinstance(part, Text):
        return {"type": "text", "text": str(part.text or "")}
    if isinstance(part, ReasoningText):
        return {
            "type": "reasoning_text",
            "text": str(getattr(part, "text", "") or ""),
            "signature": str(getattr(part, "signature", "") or ""),
            "redacted_data": str(getattr(part, "redacted_data", "") or ""),
        }
    if isinstance(part, ToolCall):
        return {
            "type": "tool_call",
            "call_id": str(part.id or ""),
            "name": str(part.name or ""),
            "args": _json_safe(part.args),
        }
    if isinstance(part, ToolResult):
        return {
            "type": "tool_result",
            "call_id": str(part.call_id or ""),
            "name": str(part.name or ""),
            "value": _json_safe(part.value),
        }
    if isinstance(part, (Image, Audio, Video, File)):
        value = str(getattr(part, "value", "") or "")
        media_ref = getattr(part, "media_ref", None)
        return {
            "type": type(part).__name__.lower(),
            "value": value,
            "mime_type": str(getattr(part, "mime_type", "") or ""),
            "size_bytes": getattr(media_ref, "size_bytes", None),
            "sha256": str(getattr(media_ref, "sha256", "") or _sha256_text(value)),
            "source_message_id": str(
                getattr(media_ref, "source_message_id", "") or ""
            ),
            "origin": str(getattr(media_ref, "origin", "") or ""),
            "persistence_policy": str(
                getattr(media_ref, "persistence_policy", "") or ""
            ),
            "duration": getattr(media_ref, "duration", None),
            "dimensions": list(getattr(media_ref, "dimensions", None) or []),
        }
    to_schema = getattr(part, "to_schema", None)
    if callable(to_schema):
        return {
            "type": "llm_usable_schema",
            "schema": _json_safe(to_schema()),
        }
    raise ContextStewardshipError(
        f"unsupported context content part: {type(part).__name__}"
    )


def _payload_record(payload: LLMPayload) -> dict[str, Any]:
    role = getattr(getattr(payload, "role", None), "value", None)
    if not isinstance(role, str) or not role:
        raise ContextStewardshipError("context payload role is invalid")
    return {
        "role": role,
        "content": [_part_record(part) for part in list(payload.content or [])],
    }


def _group_record(group: Sequence[LLMPayload], ordinal: int) -> ContextGroupRecord:
    record = {
        "schema": GROUP_SCHEMA,
        "payloads": [_payload_record(payload) for payload in group],
    }
    encoded = canonical_json(record).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return ContextGroupRecord(
        ordinal=ordinal,
        group_ref=f"ctxg_{digest}",
        utf8_bytes=len(encoded),
        payload_count=len(group),
        open_tool_chain=_has_open_tool_chain(group),
        legacy_transport_projection=any(
            is_legacy_summary_payload(payload) for payload in group
        ),
        record=record,
    )


def split_pinned_and_tail(
    payloads: Sequence[LLMPayload],
) -> tuple[list[LLMPayload], list[LLMPayload]]:
    """Split only the contiguous SYSTEM/TOOL schema prefix."""

    split_at = 0
    for payload in payloads:
        if getattr(payload, "role", None) not in {ROLE.SYSTEM, ROLE.TOOL}:
            break
        split_at += 1
    return list(payloads[:split_at]), list(payloads[split_at:])


def build_conversation_groups(
    payloads: Sequence[LLMPayload],
) -> list[list[LLMPayload]]:
    """Group by USER start without splitting an assistant/tool chain."""

    groups: list[list[LLMPayload]] = []
    for payload in payloads:
        if getattr(payload, "role", None) == ROLE.USER or not groups:
            groups.append([])
        groups[-1].append(payload)
    return groups


def _has_open_tool_chain(group: Sequence[LLMPayload]) -> bool:
    if not group:
        return False
    last = group[-1]
    if getattr(last, "role", None) == ROLE.TOOL_RESULT:
        return True
    if getattr(last, "role", None) == ROLE.ASSISTANT:
        return any(isinstance(part, ToolCall) for part in list(last.content or []))
    return False


def is_legacy_summary_payload(payload: LLMPayload) -> bool:
    """Recognize the retired copier-summary only for safe migration."""

    if getattr(payload, "role", None) != ROLE.USER:
        return False
    parts = list(getattr(payload, "content", None) or [])
    if len(parts) != 1 or not isinstance(parts[0], Text):
        return False
    text = str(parts[0].text or "")
    return (
        text.startswith(f"{LEGACY_SUMMARY_INTRO}\n{LEGACY_SUMMARY_OPEN}")
        and text.endswith(LEGACY_SUMMARY_CLOSE)
        and text.count(LEGACY_SUMMARY_OPEN) == 1
        and text.count(LEGACY_SUMMARY_CLOSE) == 1
    )


def _strict_envelope_payload(
    payload: LLMPayload,
    *,
    role: ROLE,
    opening: str,
    closing: str,
    schema: str,
) -> dict[str, Any] | None:
    if getattr(payload, "role", None) != role:
        return None
    parts = list(getattr(payload, "content", None) or [])
    if len(parts) != 1 or not isinstance(parts[0], Text):
        return None
    text = str(parts[0].text or "")
    prefix = opening + "\n"
    suffix = "\n" + closing
    if not text.startswith(prefix) or not text.endswith(suffix):
        return None
    if text.count(opening) != 1 or text.count(closing) != 1:
        return None
    try:
        decoded = json.loads(text[len(prefix) : -len(suffix)])
    except (TypeError, ValueError):
        return None
    if not isinstance(decoded, dict) or decoded.get("schema") != schema:
        return None
    return decoded


def _compression_required_part_data(part: object) -> dict[str, Any] | None:
    """Decode one exact compression control ``Text`` part."""

    if not isinstance(part, Text):
        return None
    return _strict_envelope_payload(
        LLMPayload(ROLE.USER, [part]),
        role=ROLE.USER,
        opening=COMPRESSION_REQUIRED_OPEN,
        closing=COMPRESSION_REQUIRED_CLOSE,
        schema=COMPRESSION_REQUIRED_SCHEMA,
    )


def is_compression_required_part(part: object) -> bool:
    """Return whether ``part`` is the exact technical compression envelope."""

    return _compression_required_part_data(part) is not None


def checkpoint_data(payload: LLMPayload) -> dict[str, Any] | None:
    if getattr(payload, "role", None) != ROLE.ASSISTANT:
        return None
    candidates: list[dict[str, Any]] = []
    for part in list(getattr(payload, "content", None) or []):
        if not isinstance(part, Text):
            continue
        candidate = _strict_envelope_payload(
            LLMPayload(ROLE.ASSISTANT, [part]),
            role=ROLE.ASSISTANT,
            opening=CHECKPOINT_OPEN,
            closing=CHECKPOINT_CLOSE,
            schema=CHECKPOINT_SCHEMA,
        )
        if candidate is not None:
            candidates.append(candidate)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            int(item.get("revision", -1))
            if isinstance(item.get("revision"), int)
            else -1
        ),
    )


def current_checkpoint_data(payloads: Sequence[LLMPayload]) -> dict[str, Any] | None:
    latest: dict[str, Any] | None = None
    latest_revision = -1
    for payload in payloads:
        data = checkpoint_data(payload)
        if data is None:
            continue
        revision = data.get("revision")
        if isinstance(revision, int) and revision > latest_revision:
            latest = data
            latest_revision = revision
    return latest


def build_group_manifest(
    payloads: Sequence[LLMPayload],
    *,
    exclude_latest_group: bool = True,
) -> ContextGroupManifest:
    """Build stable refs without exposing group bodies in the pressure notice."""

    # The compression-required envelope is transport control, not a subject
    # conversation group.  Ignoring it keeps the manifest stable before and
    # after the envelope is appended to the durable rolling projection.
    semantic_payloads = strip_compression_required_payloads(payloads)
    _, tail = split_pinned_and_tail(semantic_payloads)
    groups = build_conversation_groups(tail)
    if exclude_latest_group and groups:
        groups = groups[:-1]
    records = tuple(
        _group_record(group, ordinal)
        for ordinal, group in enumerate(groups, start=1)
    )
    current = current_checkpoint_data(payloads)
    revision = int(current.get("revision", 0)) if current else 0
    manifest_body = {
        "schema": MANIFEST_SCHEMA,
        "current_checkpoint_revision": revision,
        "ordered_group_refs": [record.group_ref for record in records],
    }
    return ContextGroupManifest(
        source_manifest_sha256=canonical_json_sha256(manifest_body),
        current_checkpoint_revision=revision,
        groups=records,
    )


def _model_effective_budget(model: dict[str, Any]) -> int | None:
    maximum = model.get("max_context")
    if not isinstance(maximum, int) or maximum <= 0:
        return None
    extra = model.get("extra_params")
    extra = extra if isinstance(extra, dict) else {}
    fixed = extra.get("context_reserve_tokens")
    fixed = fixed if isinstance(fixed, int) and fixed > 0 else 0
    output = model.get("max_tokens")
    output = output if isinstance(output, int) and output > 0 else 0
    ratio = extra.get("context_reserve_ratio")
    ratio = float(ratio) if isinstance(ratio, (int, float)) else 0.0
    reserve = max(fixed, output, math.floor(maximum * max(0.0, ratio)))
    effective = max(1, maximum - reserve)
    task_budget = model.get("context_tokens")
    if isinstance(task_budget, int) and task_budget > 0:
        return min(task_budget, effective)
    return effective


def _highest_pressure(
    payloads: list[LLMPayload],
    model_set: object,
) -> tuple[int, int, float] | None:
    if not isinstance(model_set, list):
        return None
    highest: tuple[int, int, float] | None = None
    for candidate in model_set:
        if not isinstance(candidate, dict):
            continue
        budget = _model_effective_budget(candidate)
        model_identifier = candidate.get("model_identifier")
        if budget is None or not isinstance(model_identifier, str) or not model_identifier:
            continue
        try:
            tokens = count_payload_tokens(
                payloads,
                model_identifier=model_identifier,
            )
        except Exception:  # noqa: BLE001,S112 - optional pressure probe
            continue
        pressure = tokens / budget
        if highest is None or pressure > highest[2]:
            highest = (tokens, budget, pressure)
    return highest


def build_context_pressure_notice(
    response: Any,
    *,
    trigger_ratio: float = DEFAULT_PRESSURE_RATIO,
    max_groups: int = DEFAULT_PRESSURE_MAX_GROUPS,
    max_bytes: int = DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
) -> Text | None:
    """Return a one-send technical notice when the task budget is under pressure."""

    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list):
        return None
    typed = [payload for payload in payloads if isinstance(payload, LLMPayload)]
    pressure = _highest_pressure(typed, getattr(response, "model_set", None))
    if pressure is None:
        upper = getattr(response, "_upper", None)
        pressure = _highest_pressure(typed, getattr(upper, "model_set", None))
    if pressure is None:
        return None
    estimated_tokens, token_budget, ratio = pressure
    if ratio < max(0.1, min(float(trigger_ratio), 0.99)):
        return None
    manifest = build_group_manifest(typed)
    visible = list(manifest.groups[: max(1, int(max_groups))])
    budget = max(256, int(max_bytes))

    def render(items: Sequence[ContextGroupRecord]) -> str:
        data = {
            "schema": PRESSURE_SCHEMA,
            "technical_only": True,
            "estimated_tokens": estimated_tokens,
            "task_token_budget": token_budget,
            "pressure_ratio": round(ratio, 6),
            "source_manifest_sha256": manifest.source_manifest_sha256,
            "current_checkpoint_revision": manifest.current_checkpoint_revision,
            "releaseable_group_count": len(manifest.groups),
            "listed_group_count": len(items),
            "unlisted_later_group_count": max(
                0,
                len(manifest.groups) - len(items),
            ),
            "releaseable_groups_in_chronological_order": [
                record.public_descriptor() for record in items
            ],
            "subject_contract": (
                "这只是容量事实，不判断哪些经历有意义，也不要求你压缩。"
                "只有你能决定是否调用 author_self_continuity_checkpoint、"
                "释放到哪个 group_ref、保留哪些 exact refs，以及给未来的自己写什么。"
                "如本轮还要回应或行动，可以把检查点动作与其他独立动作放在同一次响应中。"
            ),
        }
        return (
            PRESSURE_OPEN
            + "\n"
            + json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
            + PRESSURE_CLOSE
        )

    while True:
        text = render(visible)
        if len(text.encode("utf-8")) <= budget:
            return Text(text)
        if not visible:
            # A technical notice that cannot fit its own configured envelope
            # must disappear; it may never take budget from subject content.
            return None
        visible.pop()


def append_context_pressure_notice(response: Any, notice: Text | None) -> None:
    if notice is None:
        return
    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list):
        return
    for payload in reversed(payloads):
        if getattr(payload, "role", None) == ROLE.USER:
            with _PENDING_LOCK:
                _TRANSIENT_PRESSURE_PARTS[id(notice)] = notice
            payload.content.append(notice)
            return


def strip_context_pressure_notices(response: Any) -> None:
    """Remove only Text objects registered by the current transport send."""

    payloads = getattr(response, "payloads", None)
    if not isinstance(payloads, list):
        return
    for payload in payloads:
        if getattr(payload, "role", None) != ROLE.USER:
            continue
        kept: list[object] = []
        for part in list(payload.content or []):
            if isinstance(part, Text):
                with _PENDING_LOCK:
                    registered = _TRANSIENT_PRESSURE_PARTS.get(id(part))
                    if registered is part:
                        _TRANSIENT_PRESSURE_PARTS.pop(id(part), None)
                        continue
            kept.append(part)
        payload.content = kept  # type: ignore[assignment]


def reset_transient_context_pressure_notices() -> None:
    """Release strong identities left behind by an aborted Chatter send."""

    with _PENDING_LOCK:
        _TRANSIENT_PRESSURE_PARTS.clear()


def is_compression_required_payload(payload: LLMPayload) -> bool:
    """Recognize a USER payload carrying the durable compression control.

    The control may be its sole part or may share the current USER turn.  The
    latter is required when pressure is discovered while that USER turn is
    still open: emitting a second USER payload would make later helpers move
    the control across its maintenance assistant/tool-result chain.
    """

    return getattr(payload, "role", None) == ROLE.USER and any(
        is_compression_required_part(part)
        for part in list(getattr(payload, "content", None) or [])
    )


def has_compression_required_payload(payloads: Sequence[LLMPayload]) -> bool:
    return any(
        is_compression_required_payload(payload)
        for payload in payloads
        if isinstance(payload, LLMPayload)
    )


def strip_compression_required_payloads(
    payloads: Sequence[LLMPayload],
) -> list[LLMPayload]:
    stripped: list[LLMPayload] = []
    for payload in payloads:
        if not isinstance(payload, LLMPayload):
            continue
        original = list(getattr(payload, "content", None) or [])
        if getattr(payload, "role", None) != ROLE.USER:
            stripped.append(payload)
            continue
        content = [
            part for part in original if not is_compression_required_part(part)
        ]
        if not content:
            continue
        stripped.append(
            payload
            if len(content) == len(original)
            else LLMPayload(payload.role, content)  # type: ignore[arg-type]
        )
    return stripped


def strip_compression_maintenance_transport(
    payloads: Sequence[LLMPayload],
) -> list[LLMPayload]:
    """Remove one compression control and its response-side transport.

    Everything after the control is produced while the surface is in its
    maintenance-only mode.  Assistant/tool-result frames there are transport
    for exact-group reads and checkpoint submission, not subject continuity.
    Real USER frames that arrived during maintenance are preserved verbatim;
    pinned payloads are preserved as well.  Authoritative activity trajectories
    are not touched by this derived-context helper.
    """

    typed = [payload for payload in payloads if isinstance(payload, LLMPayload)]
    control_indexes = [
        index
        for index, payload in enumerate(typed)
        if is_compression_required_payload(payload)
    ]
    if not control_indexes:
        return typed
    if len(control_indexes) != 1:
        raise ContextStewardshipError(
            "multiple compression maintenance controls are not allowed"
        )
    control_index = control_indexes[0]
    semantic_prefix = strip_compression_required_payloads(
        typed[: control_index + 1]
    )
    preserved_suffix = [
        payload
        for payload in typed[control_index + 1 :]
        if payload.role in {ROLE.SYSTEM, ROLE.TOOL, ROLE.USER}
    ]
    return [*semantic_prefix, *preserved_suffix]


def rolling_char_estimate(
    payloads: Sequence[LLMPayload],
    estimate: Callable[[Sequence[LLMPayload]], int] | None = None,
) -> int:
    typed = [payload for payload in payloads if isinstance(payload, LLMPayload)]
    if estimate is not None:
        return int(estimate(typed))
    return _payloads_utf8_bytes(typed)


def payloads_require_compression(
    payloads: Sequence[LLMPayload],
    *,
    estimate: Callable[[Sequence[LLMPayload]], int] | None = None,
    trigger_chars: int,
) -> bool:
    typed = [payload for payload in payloads if isinstance(payload, LLMPayload)]
    if has_compression_required_payload(typed):
        return True
    return rolling_char_estimate(typed, estimate) > max(1, int(trigger_chars))


def build_compression_required_payload(
    payloads: Sequence[LLMPayload],
    *,
    max_groups: int = DEFAULT_PRESSURE_MAX_GROUPS,
    max_bytes: int = DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
    estimated_chars: int | None = None,
    trigger_chars: int | None = None,
) -> LLMPayload | None:
    """Build one durable USER list of releasable groups; no message or tool bodies."""

    typed = [payload for payload in payloads if isinstance(payload, LLMPayload)]
    manifest = build_group_manifest(typed)
    if not manifest.groups:
        return None
    visible = list(manifest.groups[: max(1, int(max_groups))])
    budget = max(256, int(max_bytes))

    def render(items: Sequence[ContextGroupRecord]) -> str:
        data = {
            "schema": COMPRESSION_REQUIRED_SCHEMA,
            "technical_only": True,
            "estimated_chars": int(estimated_chars or 0),
            "trigger_chars": int(trigger_chars or 0),
            "source_manifest_sha256": manifest.source_manifest_sha256,
            "current_checkpoint_revision": manifest.current_checkpoint_revision,
            "releaseable_group_count": len(manifest.groups),
            "listed_group_count": len(items),
            "unlisted_later_group_count": max(0, len(manifest.groups) - len(items)),
            "releaseable_groups_in_chronological_order": [
                record.public_descriptor() for record in items
            ],
            "subject_contract": (
                "滚动上下文已超过容量触发阈值。这只是组身份清单，不含正文，"
                "也不判断哪些经历有意义。在恢复普通工作之前，必须由你调用 "
                "author_self_continuity_checkpoint，自己选择释放到哪个 group_ref、"
                "保留哪些 exact refs，并亲自写下 continuity_text。"
                "需要先看原文时用 read_context_group。"
                "系统不会代写摘要，也不会丢掉旧组来硬塞进窗口。"
            ),
        }
        return (
            COMPRESSION_REQUIRED_OPEN
            + "\n"
            + json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
            + COMPRESSION_REQUIRED_CLOSE
        )

    while True:
        text = render(visible)
        if len(text.encode("utf-8")) <= budget:
            return LLMPayload(ROLE.USER, [Text(text)])
        if not visible:
            return None
        visible.pop()


def ensure_compression_required_appended(
    payloads: Sequence[LLMPayload],
    *,
    estimate: Callable[[Sequence[LLMPayload]], int] | None = None,
    trigger_chars: int,
    max_groups: int = DEFAULT_PRESSURE_MAX_GROUPS,
    max_bytes: int = DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
    force: bool = False,
) -> list[LLMPayload]:
    """Append the compression list once. Existing list bytes are not rewritten."""

    typed = [payload for payload in payloads if isinstance(payload, LLMPayload)]
    if has_compression_required_payload(typed):
        return typed
    estimated = rolling_char_estimate(typed, estimate)
    if not force and estimated <= max(1, int(trigger_chars)):
        return typed
    notice = build_compression_required_payload(
        typed,
        max_groups=max_groups,
        max_bytes=max_bytes,
        estimated_chars=estimated,
        trigger_chars=trigger_chars,
    )
    if notice is None:
        return typed
    if typed and typed[-1].role == ROLE.USER:
        # Keep the transport instruction as its own exact Text part inside the
        # open USER turn.  A standalone USER frame here can later be detached
        # and restored across maintenance replies, corrupting role order.
        return [
            *typed[:-1],
            LLMPayload(
                ROLE.USER,
                [*list(typed[-1].content or []), *list(notice.content or [])],
            ),
        ]
    return [*typed, notice]


def install_fail_closed_context_hook(request: Any) -> None:
    """Refuse kernel group-dropping instead of emitting mechanical omission."""

    context_manager = getattr(request, "context_manager", None)
    if context_manager is None or not hasattr(context_manager, "compression_hook"):
        return
    if getattr(context_manager, "compression_hook", None) is not None:
        return

    def compression_hook(
        dropped_groups: list[list[LLMPayload]],
        remaining_payloads: list[LLMPayload],
    ) -> list[LLMPayload]:
        del remaining_payloads
        if dropped_groups:
            raise LLMContextError(
                "subject rolling context exceeds the model window; "
                "mechanical group omission is not a success path. "
                "author_self_continuity_checkpoint must release groups first."
            )
        return []

    context_manager.compression_hook = compression_hook


def install_subject_context_recovery_hook(
    request: Any,
    *,
    max_groups: int = DEFAULT_PRESSURE_MAX_GROUPS,
    max_bytes: int = DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
) -> None:
    """Install a one-attempt recovery projection for subject-owned compaction.

    The kernel may omit old groups only from the payload list sent in this
    maintenance attempt.  ``LLMRequest`` deliberately retains the caller's
    complete payload list on the returned response, so no rolling state is
    released until ``author_self_continuity_checkpoint`` archives and commits
    the active subject's explicit decision.
    """

    context_manager = getattr(request, "context_manager", None)
    if context_manager is None or not hasattr(context_manager, "compression_hook"):
        return
    if getattr(context_manager, "compression_hook", None) is not None:
        return

    def compression_hook(
        dropped_groups: list[list[LLMPayload]],
        remaining_payloads: list[LLMPayload],
    ) -> list[LLMPayload]:
        if not dropped_groups:
            return []
        setattr(compression_hook, _RECOVERY_MARKER_ATTR, True)
        complete_tail = [
            payload
            for group in dropped_groups
            for payload in group
            if isinstance(payload, LLMPayload)
        ] + [
            payload
            for payload in remaining_payloads
            if isinstance(payload, LLMPayload)
        ]
        control_parts = [
            part
            for payload in complete_tail
            if payload.role == ROLE.USER
            for part in list(payload.content or [])
            if is_compression_required_part(part)
        ]
        if len(control_parts) > 1:
            raise LLMContextError(
                "multiple compression maintenance controls are not allowed"
            )
        if has_compression_required_payload(remaining_payloads):
            # The durable control envelope is already the newest retained
            # group.  Returning no replacement lets the kernel omit old groups
            # for this attempt without inventing any semantic summary.
            return []
        if control_parts:
            # The existing control binds an earlier exact manifest.  If its
            # USER group fell outside this attempt's model window, project the
            # exact control part back into the attempt instead of generating a
            # replacement whose manifest would not match retained state.
            return [LLMPayload(ROLE.USER, [control_parts[0]])]
        notice = build_compression_required_payload(
            complete_tail,
            max_groups=max_groups,
            max_bytes=max_bytes,
            estimated_chars=rolling_char_estimate(complete_tail),
            trigger_chars=0,
        )
        if notice is None:
            raise LLMContextError(
                "subject rolling context exceeds the model window and no "
                "releasable closed context group is available"
            )
        return [notice]

    setattr(compression_hook, _RECOVERY_MARKER_ATTR, False)
    context_manager.compression_hook = compression_hook


def reset_subject_context_recovery_marker(holder: Any) -> None:
    """Reset attempt-local recovery evidence before one model send."""

    context_manager = getattr(holder, "context_manager", None)
    if context_manager is None:
        context_manager = getattr(getattr(holder, "_upper", None), "context_manager", None)
    hook = getattr(context_manager, "compression_hook", None)
    if hook is not None and hasattr(hook, _RECOVERY_MARKER_ATTR):
        setattr(hook, _RECOVERY_MARKER_ATTR, False)


def is_subject_window_overflow_error(error: BaseException) -> bool:
    """True when send failed because the subject rolling chain still cannot fit."""

    if not isinstance(error, LLMContextError):
        return False
    text = str(error)
    return (
        "mechanical group omission is not a success path" in text
        or "no releasable closed context group is available" in text
    )


def consume_subject_context_recovery_marker(holder: Any) -> bool:
    """Return whether the final attempt used the bounded recovery projection."""

    context_manager = getattr(holder, "context_manager", None)
    if context_manager is None:
        context_manager = getattr(getattr(holder, "_upper", None), "context_manager", None)
    hook = getattr(context_manager, "compression_hook", None)
    used = bool(getattr(hook, _RECOVERY_MARKER_ATTR, False))
    if hook is not None and hasattr(hook, _RECOVERY_MARKER_ATTR):
        setattr(hook, _RECOVERY_MARKER_ATTR, False)
    return used


def _checkpoint_payload(
    command: SubjectCheckpointCommand,
    *,
    revision: int,
    released: Sequence[ContextGroupRecord],
    retained: Sequence[ContextGroupRecord],
    max_bytes: int,
    archive_namespace: str = ARCHIVE_NAMESPACE,
) -> tuple[str, LLMPayload]:
    checkpoint_id = "selfctx_" + command.command_sha256
    data = {
        "schema": CHECKPOINT_SCHEMA,
        "checkpoint_id": checkpoint_id,
        "revision": revision,
        "actor_consciousness_instance_id": command.actor_consciousness_instance_id,
        "command_sha256": command.command_sha256,
        "source_manifest_sha256": command.source_manifest_sha256,
        "release_through_group_ref": command.release_through_group_ref,
        "released_group_refs": [record.group_ref for record in released],
        "retained_exact_group_refs": [record.group_ref for record in retained],
        "exact_archive": {
            "namespace": str(archive_namespace or ARCHIVE_NAMESPACE),
            "state_keys": [record.group_ref for record in released],
        },
        "continuity_text": command.continuity_text,
        "continuity_text_sha256": _sha256_text(command.continuity_text),
        "statement": (
            "这是该意识实例亲自写给未来自己的连续性说明；"
            "它不是用户新消息，也不是基础设施生成的摘要。"
        ),
    }
    text = (
        CHECKPOINT_OPEN
        + "\n"
        + json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2)
        + "\n"
        + CHECKPOINT_CLOSE
    )
    encoded = text.encode("utf-8")
    if len(encoded) > max(1024, int(max_bytes)):
        raise ContextStewardshipError(
            "subject continuity checkpoint exceeds its UTF-8 byte budget"
        )
    return checkpoint_id, LLMPayload(ROLE.ASSISTANT, [Text(text)])


def _payloads_utf8_bytes(payloads: Sequence[LLMPayload]) -> int:
    records = [_payload_record(payload) for payload in payloads]
    return len(canonical_json(records).encode("utf-8"))


def _attach_checkpoint_to_current_group(
    groups: list[list[LLMPayload]],
    checkpoint_payload: LLMPayload,
) -> None:
    """Attach the subject statement to the current valid assistant turn.

    A standalone ASSISTANT payload at the beginning of the conversation would
    violate the provider tool-chain contract.  The checkpoint is authored by
    the subject, so it is embedded in the newest group's assistant payload
    (normally the tool call that authored it).  If that group has not produced
    an assistant payload yet, appending one after its USER payload is valid.
    """

    if not groups:
        raise ContextStewardshipError(
            "checkpoint has no current conversation group"
        )
    checkpoint_parts = list(checkpoint_payload.content or [])
    current = groups[-1]
    for index in range(len(current) - 1, -1, -1):
        payload = current[index]
        if getattr(payload, "role", None) != ROLE.ASSISTANT:
            continue
        parts = list(payload.content or [])
        insertion = next(
            (
                part_index
                for part_index, part in enumerate(parts)
                if isinstance(part, ToolCall)
            ),
            len(parts),
        )
        parts[insertion:insertion] = checkpoint_parts
        current[index] = LLMPayload(ROLE.ASSISTANT, parts)  # type: ignore[arg-type]
        return
    current.append(checkpoint_payload)


def prepare_subject_checkpoint(
    payloads: Sequence[LLMPayload],
    command: SubjectCheckpointCommand,
    *,
    max_checkpoint_bytes: int = DEFAULT_CHECKPOINT_MAX_BYTES,
    archive_namespace: str = ARCHIVE_NAMESPACE,
) -> PreparedSubjectCheckpoint:
    """Validate and prepare one subject-authored prefix release atomically."""

    actor = str(command.actor_consciousness_instance_id or "").strip()
    if not actor:
        raise ContextStewardshipError("checkpoint actor is missing")
    if not str(command.thought or "").strip():
        raise ContextStewardshipError("checkpoint thought must be non-empty")
    if not str(command.continuity_text or "").strip():
        raise ContextStewardshipError("continuity_text must be non-empty")
    if command.expected_revision < 0:
        raise ContextStewardshipError("expected_revision must not be negative")

    typed = [payload for payload in payloads if isinstance(payload, LLMPayload)]
    control_indexes = [
        index
        for index, payload in enumerate(typed)
        if is_compression_required_payload(payload)
    ]
    if len(control_indexes) > 1:
        raise ContextStewardshipError(
            "multiple compression maintenance controls are not allowed"
        )
    semantic_payloads = strip_compression_maintenance_transport(typed)
    if control_indexes:
        # The control binds the manifest as it existed when maintenance began.
        # USER input arriving later must survive the checkpoint, but it must
        # not retroactively change which old groups that existing command was
        # authorized to release.
        manifest_payloads = strip_compression_required_payloads(
            typed[: control_indexes[0] + 1]
        )
    else:
        manifest_payloads = semantic_payloads
    manifest = build_group_manifest(manifest_payloads)
    if manifest.source_manifest_sha256 != command.source_manifest_sha256:
        raise ContextStewardshipError("context group manifest is stale or mismatched")
    if manifest.current_checkpoint_revision != command.expected_revision:
        raise ContextStewardshipError(
            "subject continuity checkpoint revision conflict"
        )
    refs = [record.group_ref for record in manifest.groups]
    try:
        boundary = refs.index(command.release_through_group_ref)
    except ValueError as exc:
        raise ContextStewardshipError(
            "release_through_group_ref is not in the current manifest"
        ) from exc
    selected = list(manifest.groups[: boundary + 1])
    if len(set(command.retain_exact_group_refs)) != len(
        command.retain_exact_group_refs
    ):
        raise ContextStewardshipError(
            "retain_exact_group_refs must not contain duplicates"
        )
    retain_refs = tuple(dict.fromkeys(command.retain_exact_group_refs))
    unknown_retain = set(retain_refs) - {record.group_ref for record in selected}
    if unknown_retain:
        raise ContextStewardshipError(
            "retain_exact_group_refs must belong to the selected prefix"
        )
    retained = [record for record in selected if record.group_ref in retain_refs]
    released = [record for record in selected if record.group_ref not in retain_refs]
    if not released:
        raise ContextStewardshipError("checkpoint would not release any context group")
    if any(record.open_tool_chain for record in released):
        raise ContextStewardshipError("checkpoint cannot release an open tool chain")

    checkpoint_id, checkpoint_payload = _checkpoint_payload(
        command,
        revision=command.expected_revision + 1,
        released=released,
        retained=retained,
        max_bytes=max_checkpoint_bytes,
        archive_namespace=archive_namespace,
    )
    pinned, tail = split_pinned_and_tail(semantic_payloads)
    groups = build_conversation_groups(tail)
    # The manifest excludes the latest in-flight group.  Rebuild from the same
    # selected prefix and preserve every later/current group byte-for-byte.
    selected_by_ordinal = {record.ordinal: record for record in selected}
    surviving_groups: list[list[LLMPayload]] = []
    for ordinal, group in enumerate(groups, start=1):
        selected_record = selected_by_ordinal.get(ordinal)
        if selected_record is None:
            surviving_groups.append(list(group))
            continue
        if selected_record.group_ref in retain_refs:
            surviving_groups.append(list(group))
    _attach_checkpoint_to_current_group(
        surviving_groups,
        checkpoint_payload,
    )
    rebuilt = strip_compression_required_payloads(
        list(pinned) + _flatten_groups(surviving_groups)
    )
    return PreparedSubjectCheckpoint(
        command=command,
        checkpoint_id=checkpoint_id,
        revision=command.expected_revision + 1,
        checkpoint_payload=checkpoint_payload,
        payloads=rebuilt,
        released_groups=tuple(released),
        retained_groups=tuple(retained),
        before_utf8_bytes=_payloads_utf8_bytes(typed),
        after_utf8_bytes=_payloads_utf8_bytes(rebuilt),
    )


def queue_subject_checkpoint(
    command: SubjectCheckpointCommand,
    *,
    runtime_key: str = CHATTER_RUNTIME_KEY,
) -> bool:
    """Queue one validated command; return True for an idempotent replay."""

    key = _pending_key(command.actor_consciousness_instance_id, runtime_key)
    with _PENDING_LOCK:
        existing = _PENDING_CHECKPOINTS.get(key)
        if existing is not None:
            if existing.command_sha256 == command.command_sha256:
                return True
            raise ContextStewardshipError(
                "another subject continuity checkpoint is already pending"
            )
        _PENDING_CHECKPOINTS[key] = command
    return False


def reset_pending_subject_checkpoint(
    actor_consciousness_instance_id: str,
    *,
    runtime_key: str = CHATTER_RUNTIME_KEY,
) -> None:
    key = _pending_key(actor_consciousness_instance_id, runtime_key)
    with _PENDING_LOCK:
        _PENDING_CHECKPOINTS.pop(key, None)


def apply_pending_subject_checkpoint(
    actor_consciousness_instance_id: str,
    payloads: Sequence[LLMPayload],
    *,
    max_checkpoint_bytes: int = DEFAULT_CHECKPOINT_MAX_BYTES,
    runtime_key: str = CHATTER_RUNTIME_KEY,
    archive_namespace: str = ARCHIVE_NAMESPACE,
) -> ContextStewardshipResult:
    """Install a queued command at a closed tool boundary of one surface."""

    actor = str(actor_consciousness_instance_id or "").strip()
    typed = [payload for payload in payloads if isinstance(payload, LLMPayload)]
    before = _payloads_utf8_bytes(typed)
    key = _pending_key(actor, runtime_key)
    with _PENDING_LOCK:
        command = _PENDING_CHECKPOINTS.get(key)
    if command is None:
        return ContextStewardshipResult(
            triggered=False,
            payloads=typed,
            before_utf8_bytes=before,
            after_utf8_bytes=before,
        )
    try:
        prepared = prepare_subject_checkpoint(
            typed,
            command,
            max_checkpoint_bytes=max_checkpoint_bytes,
            archive_namespace=archive_namespace,
        )
    finally:
        with _PENDING_LOCK:
            current = _PENDING_CHECKPOINTS.get(key)
            if current is not None and current.command_sha256 == command.command_sha256:
                _PENDING_CHECKPOINTS.pop(key, None)
    return ContextStewardshipResult(
        triggered=True,
        payloads=prepared.payloads,
        before_utf8_bytes=prepared.before_utf8_bytes,
        after_utf8_bytes=prepared.after_utf8_bytes,
        released_groups=len(prepared.released_groups),
        checkpoint_id=prepared.checkpoint_id,
        revision=prepared.revision,
        stop_reason="subject_checkpoint_installed",
    )


def _archive_payload(record: ContextGroupRecord) -> dict[str, Any]:
    return {
        "schema": ARCHIVE_SCHEMA,
        "group_ref": record.group_ref,
        "group_sha256": record.group_ref.removeprefix("ctxg_"),
        "utf8_bytes": record.utf8_bytes,
        "record": record.record,
    }


async def archive_context_groups(
    records: Sequence[ContextGroupRecord],
    *,
    service: Any | None,
    workspace_path: str,
    namespace: str = ARCHIVE_NAMESPACE,
    local_subdir: str = CHATTER_ARCHIVE_SUBDIR,
) -> None:
    """Persist exact released groups before their prompt projection is changed."""

    if not records:
        return
    archive_namespace = str(namespace or ARCHIVE_NAMESPACE).strip() or ARCHIVE_NAMESPACE
    archive_subdir = str(local_subdir or CHATTER_ARCHIVE_SUBDIR).strip() or CHATTER_ARCHIVE_SUBDIR
    store = None
    if service is not None:
        getter = getattr(service, "runtime_state_store", None)
        if callable(getter):
            store = getter()
    if store is not None:
        from ..storage.runtime_contracts import RuntimeStateConflict

        for record in records:
            payload = _archive_payload(record)
            encoded = canonical_json(payload).encode("utf-8")
            if len(encoded) > ARCHIVE_MAX_BYTES:
                raise ContextStewardshipError(
                    "context group exceeds the immutable archive byte limit"
                )
            existing = await store.get_state(archive_namespace, record.group_ref)
            if existing is not None:
                if existing.payload != payload:
                    raise ContextStewardshipError(
                        "content-addressed context archive mismatch"
                    )
                continue
            try:
                await store.put_state(
                    namespace=archive_namespace,
                    state_key=record.group_ref,
                    expected_revision=0,
                    schema_version=1,
                    payload=payload,
                )
            except RuntimeStateConflict:
                existing = await store.get_state(archive_namespace, record.group_ref)
                if existing is None or existing.payload != payload:
                    raise
        return

    workspace = str(workspace_path or "").strip()
    if not workspace:
        raise ContextStewardshipError(
            "local context archive workspace_path is missing"
        )
    root = Path(workspace).expanduser() / "runtime" / archive_subdir
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    for record in records:
        payload = _archive_payload(record)
        text = canonical_json(payload)
        if len(text.encode("utf-8")) > ARCHIVE_MAX_BYTES:
            raise ContextStewardshipError(
                "context group exceeds the immutable archive byte limit"
            )
        path = root / f"{record.group_ref}.json"
        if path.exists():
            existing = await asyncio.to_thread(path.read_text, encoding="utf-8")
            if existing != text:
                raise ContextStewardshipError(
                    "content-addressed local context archive mismatch"
                )
            continue
        tmp = path.with_suffix(f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            await asyncio.to_thread(tmp.write_text, text, encoding="utf-8")
            await asyncio.to_thread(os.replace, tmp, path)
        finally:
            try:
                await asyncio.to_thread(tmp.unlink, missing_ok=True)
            except OSError:
                # A stranded temporary file does not weaken the immutable
                # content-addressed destination.  A later maintenance pass may
                # remove it without interpreting subject content.
                pass


async def read_context_group_archive(
    group_ref: str,
    *,
    service: Any | None,
    workspace_path: str,
    namespace: str = ARCHIVE_NAMESPACE,
    local_subdir: str = CHATTER_ARCHIVE_SUBDIR,
) -> dict[str, Any]:
    ref = str(group_ref or "").strip()
    if re.fullmatch(r"ctxg_[0-9a-f]{64}", ref) is None:
        raise ContextStewardshipError("group_ref is invalid")
    archive_namespace = str(namespace or ARCHIVE_NAMESPACE).strip() or ARCHIVE_NAMESPACE
    archive_subdir = str(local_subdir or CHATTER_ARCHIVE_SUBDIR).strip() or CHATTER_ARCHIVE_SUBDIR
    store = None
    if service is not None:
        getter = getattr(service, "runtime_state_store", None)
        if callable(getter):
            store = getter()
    payload: dict[str, Any] | None = None
    if store is not None:
        stored = await store.get_state(archive_namespace, ref)
        if stored is None:
            raise ContextGroupArchiveNotFound(
                "context group archive was not found"
            )
        candidate = getattr(stored, "payload", None)
        if not isinstance(candidate, dict):
            raise ContextStewardshipError("context group archive is malformed")
        payload = _json_safe(candidate)
    else:
        workspace = str(workspace_path or "").strip()
        if not workspace:
            raise ContextStewardshipError(
                "local context archive workspace_path is missing"
            )
        root = Path(workspace).expanduser() / "runtime" / archive_subdir
        path = root / f"{ref}.json"
        if not path.exists() or not path.is_file():
            raise ContextGroupArchiveNotFound(
                "context group archive was not found"
            )
        try:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            candidate = json.loads(raw)
        except (OSError, UnicodeError, ValueError) as exc:
            raise ContextStewardshipError(
                "context group archive could not be read"
            ) from exc
        if not isinstance(candidate, dict):
            raise ContextStewardshipError("context group archive is malformed")
        payload = candidate

    if payload.get("schema") != ARCHIVE_SCHEMA or payload.get("group_ref") != ref:
        raise ContextStewardshipError("context group archive identity mismatch")
    record = payload.get("record")
    if not isinstance(record, dict) or record.get("schema") != GROUP_SCHEMA:
        raise ContextStewardshipError("context group archive record is malformed")
    encoded = canonical_json(record).encode("utf-8")
    expected_digest = ref.removeprefix("ctxg_")
    if hashlib.sha256(encoded).hexdigest() != expected_digest:
        raise ContextStewardshipError("context group archive digest mismatch")
    if payload.get("group_sha256") != expected_digest:
        raise ContextStewardshipError("context group archive digest metadata mismatch")
    if payload.get("utf8_bytes") != len(encoded):
        raise ContextStewardshipError("context group archive byte metadata mismatch")
    return payload


def _utf8_page(text: str, *, offset_bytes: int, max_bytes: int) -> tuple[str, int, bool]:
    encoded = text.encode("utf-8")
    offset = int(offset_bytes)
    if offset < 0 or offset > len(encoded):
        raise ContextStewardshipError("offset_bytes is outside the archived group")
    try:
        encoded[:offset].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ContextStewardshipError("offset_bytes is not on a UTF-8 boundary") from exc
    if offset == len(encoded):
        return "", offset, True
    end = min(len(encoded), offset + max(1, int(max_bytes)))
    while end > offset:
        try:
            chunk = encoded[offset:end].decode("utf-8")
            return chunk, end, end == len(encoded)
        except UnicodeDecodeError:
            end -= 1
    raise ContextStewardshipError("max_bytes cannot contain one UTF-8 code point")


def _plugin_service(plugin: Any) -> Any | None:
    return getattr(plugin, "service", None)


def _plugin_workspace(plugin: Any) -> str:
    config = getattr(plugin, "config", None)
    configured = str(
        getattr(getattr(config, "settings", None), "workspace_path", "") or ""
    ).strip()
    if configured:
        return configured
    service = _plugin_service(plugin)
    return str(getattr(service, "_workspace_path", "") or "").strip()


def _runtime_key_of(component: Any) -> str:
    explicit = str(getattr(component, "_context_runtime_key", "") or "").strip()
    if explicit:
        return explicit
    return CHATTER_RUNTIME_KEY


def _live_window_payloads(runtime_key: str) -> list[LLMPayload] | None:
    window = get_live_context(runtime_key)
    if window is not None and isinstance(window.payloads, list):
        return [payload for payload in window.payloads if isinstance(payload, LLMPayload)]
    if runtime_key == CHATTER_RUNTIME_KEY:
        from .chatter import LifeChatter

        runtime = getattr(LifeChatter, "_GLOBAL_RUNTIME", None)
        response = getattr(runtime, "response", None)
        payloads = getattr(response, "payloads", None)
        if isinstance(payloads, list):
            return [payload for payload in payloads if isinstance(payload, LLMPayload)]
    return None


def _live_context_group(
    group_ref: str,
    *,
    runtime_key: str = CHATTER_RUNTIME_KEY,
) -> tuple[ContextGroupRecord, LiveContextWindow | None] | tuple[None, None]:
    """Resolve a ref from the current private runtime without broad history reads."""

    payloads = _live_window_payloads(runtime_key)
    if not payloads:
        return None, None
    manifest = build_group_manifest(payloads, exclude_latest_group=False)
    record = next(
        (item for item in manifest.groups if item.group_ref == group_ref),
        None,
    )
    if record is None:
        return None, None
    window = get_live_context(runtime_key)
    if window is None:
        namespace, subdir = archive_target_for_runtime(runtime_key)
        window = LiveContextWindow(
            runtime_key=runtime_key,
            payloads=payloads,
            archive_namespace=namespace,
            local_archive_subdir=subdir,
        )
    return record, window


class LifeAuthorSelfContinuityCheckpointAction(BaseAction):
    """Let the active subject author its own continuity checkpoint."""

    action_name = "author_self_continuity_checkpoint"
    action_description = (
        "滚动上下文进入压缩回合时，由你亲自决定释放哪些旧工作组，并把你希望未来的自己继续"
        "知道的内容写成 continuity_text。系统不会替你概括、挑选重要内容或改写正文。"
        "必须原样使用压缩清单里的 manifest/revision/group_ref；动作先归档精确旧组，随后才在安全边界安装。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_chatter", "life_engine_internal"]

    async def execute(
        self,
        thought: Annotated[
            str,
            "你为什么选择此刻建立连续性检查点的真实思考；非空，只记录为意识活动，不自动变成 continuity_text。",
        ],
        continuity_text: Annotated[
            str,
            "你亲自写给未来自己的连续性说明。开放自然语言，系统不会补全、摘要或改写。",
        ],
        source_manifest_sha256: Annotated[
            str,
            "当前 context_compression_required 清单给出的 source_manifest_sha256，必须精确复制。",
        ],
        expected_revision: Annotated[
            int,
            "当前压缩清单给出的 current_checkpoint_revision。",
        ],
        release_through_group_ref: Annotated[
            str,
            "按时间顺序释放到哪个 group_ref（含该组）；必须来自当前清单。",
        ],
        retain_exact_group_refs: Annotated[
            list[str],
            "在所选释放前缀中仍要原样保留在工作上下文里的 group_ref；没有则传空列表。",
        ],
    ) -> tuple[bool, str]:
        origin = self._action_origin_extra()
        actor = str(origin.get("consciousness_instance_id") or "").strip()
        if not actor:
            return False, "当前调用没有可验证的 active consciousness instance"
        runtime_key = _runtime_key_of(self)
        archive_namespace, archive_subdir = archive_target_for_runtime(runtime_key)
        command = SubjectCheckpointCommand(
            actor_consciousness_instance_id=actor,
            thought=str(thought or ""),
            continuity_text=str(continuity_text or ""),
            source_manifest_sha256=str(source_manifest_sha256 or "").strip(),
            expected_revision=int(expected_revision),
            release_through_group_ref=str(release_through_group_ref or "").strip(),
            retain_exact_group_refs=tuple(
                str(item or "").strip()
                for item in (retain_exact_group_refs or [])
                if str(item or "").strip()
            ),
        )
        try:
            payloads = _live_window_payloads(runtime_key)
            if not isinstance(payloads, list):
                raise ContextStewardshipError(
                    "active rolling context is unavailable"
                )
            config = getattr(self.plugin, "config", None)
            chatter = getattr(config, "chatter", None)
            max_bytes = int(
                getattr(
                    chatter,
                    "self_continuity_checkpoint_max_bytes",
                    DEFAULT_CHECKPOINT_MAX_BYTES,
                )
                or DEFAULT_CHECKPOINT_MAX_BYTES
            )
            prepared = prepare_subject_checkpoint(
                payloads,
                command,
                max_checkpoint_bytes=max_bytes,
                archive_namespace=archive_namespace,
            )
            await archive_context_groups(
                prepared.released_groups,
                service=_plugin_service(self.plugin),
                workspace_path=_plugin_workspace(self.plugin),
                namespace=archive_namespace,
                local_subdir=archive_subdir,
            )
            idempotent = queue_subject_checkpoint(
                command,
                runtime_key=runtime_key,
            )
        except ContextStewardshipError as exc:
            return False, str(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - content-neutral boundary result
            return False, f"连续性检查点归档失败: error_type={type(exc).__name__}"
        status = "已存在相同待安装命令" if idempotent else "已归档并等待安全边界安装"
        return (
            True,
            (
                f"{status}: checkpoint_id={prepared.checkpoint_id} "
                f"released_groups={len(prepared.released_groups)} revision={prepared.revision}"
            ),
        )


class LifeReadContextGroupTool(BaseTool):
    """Read an exact archived prompt group by immutable content reference."""

    tool_name = "read_context_group"
    tool_description = (
        "按 subject_self_continuity_checkpoint 或压缩清单中的 ctxg_ 引用，"
        "分页读取精确旧上下文组。尚在当前私有运行态的组会先按内容地址归档再读取；"
        "这不是摘要，每页有 UTF-8 字节硬上限。"
    )
    chatter_allow: ClassVar[list[str]] = ["life_chatter", "life_engine_internal"]

    async def execute(
        self,
        group_ref: Annotated[str, "检查点或压缩清单中列出的 ctxg_ 内容引用。"],
        offset_bytes: Annotated[
            int,
            "从哪个 UTF-8 字节位置继续读取；首次为 0，后续使用 next_offset_bytes。",
        ] = 0,
        max_bytes: Annotated[
            int,
            "本页最大 UTF-8 字节数；0 使用默认值，任何值都不会突破工具硬上限。",
        ] = 0,
    ) -> tuple[bool, dict[str, Any]]:
        try:
            requested = int(max_bytes or DEFAULT_ARCHIVE_READ_BYTES)
            offset = int(offset_bytes)
            if requested < 0:
                raise ContextStewardshipError("max_bytes must not be negative")
            budget = max(256, min(requested, MAX_ARCHIVE_READ_BYTES))
            service = _plugin_service(self.plugin)
            workspace = _plugin_workspace(self.plugin)
            runtime_key = _runtime_key_of(self)
            archive_namespace, archive_subdir = archive_target_for_runtime(runtime_key)
            try:
                archive = await read_context_group_archive(
                    group_ref,
                    service=service,
                    workspace_path=workspace,
                    namespace=archive_namespace,
                    local_subdir=archive_subdir,
                )
            except ContextGroupArchiveNotFound:
                live_record, window = _live_context_group(
                    str(group_ref or "").strip(),
                    runtime_key=runtime_key,
                )
                if live_record is None or window is None:
                    raise
                await archive_context_groups(
                    [live_record],
                    service=service,
                    workspace_path=workspace,
                    namespace=window.archive_namespace,
                    local_subdir=window.local_archive_subdir,
                )
                archive = await read_context_group_archive(
                    group_ref,
                    service=service,
                    workspace_path=workspace,
                    namespace=window.archive_namespace,
                    local_subdir=window.local_archive_subdir,
                )
            exact_text = canonical_json(archive["record"])
            chunk, next_offset, complete = _utf8_page(
                exact_text,
                offset_bytes=offset,
                max_bytes=budget,
            )
        except ContextStewardshipError as exc:
            return False, {
                "schema": "elysium.context_group_read_error.v1",
                "error": str(exc),
            }
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - content-neutral tool boundary
            return False, {
                "schema": "elysium.context_group_read_error.v1",
                "error": f"archive read failed: error_type={type(exc).__name__}",
            }
        return True, {
            "schema": "elysium.context_group_read_page.v1",
            "group_ref": str(group_ref),
            "group_sha256": archive["group_sha256"],
            "original_bytes": len(exact_text.encode("utf-8")),
            "offset_bytes": offset,
            "delivered_bytes": len(chunk.encode("utf-8")),
            "next_offset_bytes": next_offset,
            "complete": complete,
            "content": chunk,
        }


def _mechanical_omission_text(
    records: Sequence[ContextGroupRecord],
    *,
    max_group_refs: int,
    max_bytes: int,
) -> str:
    all_refs = [record.group_ref for record in records]
    listed = list(records[-max(1, int(max_group_refs)) :])
    budget = max(256, int(max_bytes))

    def render(items: Sequence[ContextGroupRecord]) -> str:
        data = {
            "schema": OMISSION_SCHEMA,
            "technical_only": True,
            "omitted_group_count": len(records),
            "ordered_group_refs_sha256": canonical_json_sha256(all_refs),
            "listed_group_count": len(items),
            "unlisted_earlier_group_count": max(0, len(records) - len(items)),
            "groups": [record.public_descriptor() for record in items],
            "authority_note": (
                "上下文组正文未被系统摘要；完整意识活动仍以 Life Event/trajectory 为准。"
            ),
        }
        return (
            OMISSION_OPEN
            + "\n"
            + json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            + OMISSION_CLOSE
        )

    while listed:
        text = render(listed)
        if len(text.encode("utf-8")) <= budget:
            return text
        listed.pop(0)
    text = render(())
    if len(text.encode("utf-8")) <= budget:
        return text
    minimal = {
        "schema": OMISSION_SCHEMA,
        "omitted_group_count": len(records),
        "ordered_group_refs_sha256": canonical_json_sha256(all_refs),
    }
    text = (
        OMISSION_OPEN
        + "\n"
        + json.dumps(minimal, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
        + OMISSION_CLOSE
    )
    if len(text.encode("utf-8")) > budget:
        raise ContextStewardshipError(
            "mechanical omission metadata cannot fit its byte budget"
        )
    return text


def build_mechanical_omission_payloads(
    dropped_groups: Sequence[Sequence[LLMPayload]],
    *,
    max_group_refs: int = DEFAULT_PRESSURE_MAX_GROUPS,
    max_bytes: int = DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
) -> list[LLMPayload]:
    """Project omitted groups as hashes and refs only."""

    records = tuple(
        _group_record(group, ordinal)
        for ordinal, group in enumerate(dropped_groups, start=1)
        if group
    )
    if not records:
        return []
    text = _mechanical_omission_text(
        records,
        max_group_refs=max_group_refs,
        max_bytes=max_bytes,
    )
    return [LLMPayload(ROLE.USER, [Text(text)])]


def _flatten_groups(groups: Sequence[Sequence[LLMPayload]]) -> list[LLMPayload]:
    return [payload for group in groups for payload in group]


def mechanically_bound_payloads(
    payloads: Sequence[LLMPayload],
    *,
    estimate: Callable[[Sequence[LLMPayload]], int],
    hard_budget: int,
    reference_max_groups: int = DEFAULT_PRESSURE_MAX_GROUPS,
    reference_max_bytes: int = DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES,
) -> tuple[ContextStewardshipResult, tuple[ContextGroupRecord, ...]]:
    """Apply a content-neutral emergency bound and return exact dropped records.

    Production send and snapshot paths must not use this as a successful
    compression.  Subject-authored checkpoints are the only way to release
    old groups from a live rolling chain.
    """

    typed = [payload for payload in payloads if isinstance(payload, LLMPayload)]
    before_bytes = _payloads_utf8_bytes(typed)
    budget = max(1, int(hard_budget))
    if estimate(typed) <= budget:
        return (
            ContextStewardshipResult(
                triggered=False,
                payloads=typed,
                before_utf8_bytes=before_bytes,
                after_utf8_bytes=before_bytes,
            ),
            (),
        )
    pinned, tail = split_pinned_and_tail(typed)
    kept = build_conversation_groups(tail)
    dropped: list[list[LLMPayload]] = []
    candidate = list(typed)
    while kept and estimate(candidate) > budget:
        dropped.append(kept.pop(0))
        omission = build_mechanical_omission_payloads(
            dropped,
            max_group_refs=reference_max_groups,
            max_bytes=reference_max_bytes,
        )
        candidate = list(pinned) + omission + _flatten_groups(kept)
    if estimate(candidate) > budget:
        # A pinned schema prefix can exceed a snapshot-only budget.  Never
        # fabricate a semantic summary merely to satisfy the number.  If even
        # content-neutral metadata cannot fit, persist an empty projection;
        # callers still receive every exact dropped record for archival.
        minimal = build_mechanical_omission_payloads(
            dropped,
            max_group_refs=reference_max_groups,
            max_bytes=reference_max_bytes,
        )
        candidate = minimal if estimate(minimal) <= budget else []
    records = tuple(
        _group_record(group, ordinal)
        for ordinal, group in enumerate(dropped, start=1)
        if group
    )
    after_bytes = _payloads_utf8_bytes(candidate)
    return (
        ContextStewardshipResult(
            triggered=True,
            payloads=candidate,
            before_utf8_bytes=before_bytes,
            after_utf8_bytes=after_bytes,
            released_groups=len(records),
            stop_reason="mechanical_hard_budget",
        ),
        records,
    )


__all__ = [
    "ARCHIVE_NAMESPACE",
    "CHATTER_ARCHIVE_SUBDIR",
    "CHATTER_RUNTIME_KEY",
    "CHECKPOINT_CLOSE",
    "CHECKPOINT_OPEN",
    "COMPRESSION_REQUIRED_CLOSE",
    "COMPRESSION_REQUIRED_OPEN",
    "DEFAULT_CHECKPOINT_MAX_BYTES",
    "DEFAULT_EMERGENCY_REFERENCE_MAX_BYTES",
    "DEFAULT_PRESSURE_MAX_GROUPS",
    "DEFAULT_PRESSURE_RATIO",
    "HEARTBEAT_ARCHIVE_NAMESPACE",
    "HEARTBEAT_ARCHIVE_SUBDIR",
    "HEARTBEAT_RUNTIME_KEY",
    "OMISSION_CLOSE",
    "OMISSION_OPEN",
    "PRESSURE_CLOSE",
    "PRESSURE_OPEN",
    "ContextGroupManifest",
    "ContextGroupRecord",
    "ContextStewardshipError",
    "ContextStewardshipResult",
    "LifeAuthorSelfContinuityCheckpointAction",
    "LifeReadContextGroupTool",
    "LiveContextWindow",
    "SubjectCheckpointCommand",
    "append_context_pressure_notice",
    "apply_pending_subject_checkpoint",
    "archive_context_groups",
    "archive_target_for_runtime",
    "build_compression_required_payload",
    "build_context_pressure_notice",
    "build_conversation_groups",
    "build_group_manifest",
    "build_mechanical_omission_payloads",
    "checkpoint_data",
    "consume_subject_context_recovery_marker",
    "current_checkpoint_data",
    "ensure_compression_required_appended",
    "get_live_context",
    "has_compression_required_payload",
    "install_fail_closed_context_hook",
    "install_subject_context_recovery_hook",
    "is_compression_required_part",
    "is_compression_required_payload",
    "is_subject_window_overflow_error",
    "is_legacy_summary_payload",
    "mechanically_bound_payloads",
    "payloads_require_compression",
    "prepare_subject_checkpoint",
    "queue_subject_checkpoint",
    "read_context_group_archive",
    "register_live_context",
    "reset_pending_subject_checkpoint",
    "reset_subject_context_recovery_marker",
    "reset_transient_context_pressure_notices",
    "rolling_char_estimate",
    "split_pinned_and_tail",
    "strip_compression_maintenance_transport",
    "strip_compression_required_payloads",
    "strip_context_pressure_notices",
    "unregister_live_context",
]
