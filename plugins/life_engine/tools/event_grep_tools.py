"""life_engine event ledger grep tools."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from src.app.plugin_system.api import log_api
from src.app.plugin_system.base import BaseTool

from ..service import LifeEngineService
from ..service.event_builder import EventType
from .bounded_projection import (
    project_bounded_items,
    project_bounded_text,
    sha256_json,
    utf8_prefix,
)

logger = log_api.get_logger("life_engine.event_grep")

_DEFAULT_LIMIT = 12
_MAX_LIMIT = 80
_DEFAULT_SCAN_LIMIT = 2000
_SCAN_PAGE = 500
_EXCERPT_MAX_BYTES = 512
_TEXT_FIELDS = (
    "content",
    "sender",
    "source",
    "source_detail",
    "tool_name",
    "tool_args",
    "stream_id",
    "content_type",
    "occurrence_id",
)
_DELIVERY_STATUS_TYPES = {
    "failed": "chat.message.delivery_failed",
    "unknown": "chat.message.delivery_unknown",
    "confirmed": "chat.message.delivery_confirmed",
    "requested": "chat.message.send_requested",
}
_EVENT_TYPE_ALIASES: dict[str, set[str]] = {
    "thought": {
        "heartbeat",
        "heartbeat_reply",
        "conscious_activity",
        "conscious_activity_model_turn",
        "chatter_inner_monologue",
        "inner_dialogue",
        "inner_dialogue_return",
        "inner_dialogue_return_delivery",
    },
    "tool": {
        "tool_call",
        "tool_result",
        "conscious_activity_tool_call",
    },
    "chat": {
        "message",
        "chat.message.received",
        "chat.message.delivery_failed",
        "chat.message.delivery_unknown",
        "chat.message.delivery_confirmed",
        "chat.message.send_requested",
    },
}


def _event_type_value(event: Any) -> str:
    event_type = getattr(event, "event_type", "")
    value = getattr(event_type, "value", event_type)
    return str(value or "").strip().lower()


def _authoritative_event_content(event: Any) -> str:
    """Return the complete immutable body instead of its display summary."""

    return str(
        getattr(event, "raw_content", None)
        or getattr(event, "content", "")
        or ""
    )


def _metadata(event: Any) -> dict[str, Any]:
    value = getattr(event, "metadata", None)
    return value if isinstance(value, dict) else {}


def _event_to_payload(event: Any, *, ledger_source: str) -> dict[str, Any]:
    metadata = _metadata(event)
    content = _authoritative_event_content(event)
    occurrence_id = str(
        getattr(event, "occurrence_id", "") or getattr(event, "event_id", "") or ""
    )
    ingest_position = int(getattr(event, "sequence", 0) or 0)
    tool_args = getattr(event, "tool_args", None)
    if tool_args is None:
        tool_args = metadata.get("tool_args")
    return {
        "event_id": str(getattr(event, "event_id", "") or ""),
        "event_type": _event_type_value(event),
        "legacy_event_type": str(metadata.get("legacy_event_type") or ""),
        "channel": str(getattr(event, "channel", "") or metadata.get("channel") or ""),
        "timestamp": str(getattr(event, "timestamp", "") or ""),
        "sequence": ingest_position,
        "ingest_position": ingest_position,
        "source": str(getattr(event, "source", "") or ""),
        "source_detail": str(
            getattr(event, "source_detail", None) or metadata.get("source_detail") or ""
        ),
        "content": content,
        "content_type": str(
            getattr(event, "content_type", None) or metadata.get("content_type") or ""
        ),
        "sender": str(
            getattr(event, "sender", None) or metadata.get("sender") or ""
        ),
        "sender_id": str(
            getattr(event, "sender_id", None) or metadata.get("sender_id") or ""
        ),
        "canonical_person_key": str(
            getattr(event, "canonical_person_key", None)
            or metadata.get("canonical_person_key")
            or ""
        ),
        "actor_id": str(metadata.get("actor_id") or ""),
        "chat_type": str(
            getattr(event, "chat_type", None) or metadata.get("chat_type") or ""
        ),
        "stream_id": str(getattr(event, "stream_id", "") or ""),
        "heartbeat_index": getattr(event, "heartbeat_index", None),
        "heartbeat_run_id": str(
            getattr(event, "heartbeat_run_id", None)
            or metadata.get("heartbeat_run_id")
            or ""
        ),
        "tool_name": str(
            getattr(event, "tool_name", None) or metadata.get("tool_name") or ""
        ),
        "tool_args": tool_args if isinstance(tool_args, dict) else {},
        "tool_success": getattr(event, "tool_success", None)
        if getattr(event, "tool_success", None) is not None
        else metadata.get("tool_success"),
        "occurrence_id": occurrence_id,
        "source_instance_id": str(
            getattr(event, "source_instance_id", "")
            or metadata.get("source_instance_id")
            or ""
        ),
        "causation_id": str(
            getattr(event, "causation_id", "") or metadata.get("causation_id") or ""
        ),
        "correlation_id": str(
            getattr(event, "correlation_id", "")
            or metadata.get("correlation_id")
            or ""
        ),
        "call_id": str(
            getattr(event, "call_id", None) or metadata.get("call_id") or ""
        ),
        "parent_event_id": str(
            getattr(event, "parent_event_id", None)
            or metadata.get("parent_event_id")
            or ""
        ),
        "content_ref": str(getattr(event, "content_ref", "") or "")
        or (f"life-event-occurrence:{occurrence_id}" if occurrence_id else ""),
        "ledger_source": ledger_source,
    }


def _excerpt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    content = str(payload.get("content") or "")
    encoded = content.encode("utf-8")
    excerpted = len(encoded) > _EXCERPT_MAX_BYTES
    clipped = utf8_prefix(content, _EXCERPT_MAX_BYTES) if excerpted else content
    occurrence_id = str(payload.get("occurrence_id") or "")
    return {
        **payload,
        "content": clipped,
        "excerpt": excerpted,
        "excerpt_ref": (
            str(payload.get("content_ref") or "")
            or (f"life-event-occurrence:{occurrence_id}" if occurrence_id else "")
        ),
        "read_with": "nucleus_read_event" if excerpted or occurrence_id else "",
    }


def _haystack(payload: dict[str, Any], fields: list[str]) -> str:
    parts: list[str] = []
    for field in fields:
        value = payload.get(field)
        if value is None:
            continue
        if isinstance(value, (dict, list, tuple, set)):
            parts.append(str(value))
        else:
            parts.append(str(value))
    return "\n".join(parts)


def _compile_pattern(
    query: str, *, use_regex: bool, case_insensitive: bool
) -> re.Pattern[str] | None:
    text = str(query or "")
    if not text.strip():
        return None
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(query if use_regex else re.escape(query), flags)


def _normalize_strings(values: list[str] | None) -> set[str]:
    return {
        str(item or "").strip().lower()
        for item in (values or [])
        if str(item or "").strip()
    }


def _expand_event_types(event_types: list[str] | None) -> tuple[set[str], bool]:
    values: set[str] = set()
    match_minecraft = False
    for item in event_types or []:
        text = str(item or "").strip().lower()
        if not text:
            continue
        if text == "minecraft":
            match_minecraft = True
            continue
        aliases = _EVENT_TYPE_ALIASES.get(text)
        if aliases:
            values.update(aliases)
            continue
        try:
            text = EventType(text).value
        except ValueError:
            pass
        values.add(text)
    return values, match_minecraft


def _is_life_internal_payload(payload: dict[str, Any]) -> bool:
    event_type = str(payload.get("event_type") or "").strip().lower()
    if event_type in {
        "heartbeat",
        "tool_call",
        "tool_result",
        "conscious_activity",
        "agent_result",
    }:
        return True
    source = str(payload.get("source") or "").strip().lower()
    content_type = str(payload.get("content_type") or "").strip().lower()
    stream_id = str(payload.get("stream_id") or "").strip()
    if source in {"life_engine", "life_chatter"}:
        return True
    return not stream_id and content_type in {
        "proactive_opportunity",
        "dfc_message",
        "direct_message",
        "inner_dialogue",
        "inner_dialogue_return",
        "heartbeat_reply",
        "conscious_activity_model_turn",
    }


def _parse_iso(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid timestamp: {text}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def _relative_after(last_hours: int | None, last_days: int | None) -> str | None:
    hours = int(last_hours or 0)
    days = int(last_days or 0)
    if hours < 0 or days < 0:
        raise ValueError("last_hours and last_days must not be negative")
    if hours == 0 and days == 0:
        return None
    delta = timedelta(hours=hours, days=days)
    return (datetime.now(UTC) - delta).isoformat()


def _in_string_set(value: str, allowed: set[str]) -> bool:
    return str(value or "").strip().lower() in allowed


def _person_haystack(payload: dict[str, Any]) -> str:
    return "\n".join(
        str(payload.get(key) or "")
        for key in ("sender", "sender_id", "canonical_person_key", "actor_id")
    )


def _matches_filters(
    payload: dict[str, Any],
    *,
    stream_filter: set[str],
    include_life_internal: bool,
    type_filter: set[str],
    match_minecraft: bool,
    exclude_types: set[str],
    exclude_sources: set[str],
    channels: set[str],
    sources: set[str],
    chat_types: set[str],
    content_types: set[str],
    instances: set[str],
    person: str,
    sender_ids: set[str],
    senders: set[str],
    tool_names: set[str],
    tool_success: bool | None,
    call_id: str,
    parent_event_id: str,
    causation_ids: set[str],
    correlation_ids: set[str],
    heartbeat_run_ids: set[str],
    occurrence_ids: set[str],
    delivery_types: set[str],
) -> bool:
    event_type = str(payload.get("event_type") or "").strip().lower()
    content_type = str(payload.get("content_type") or "").strip().lower()
    legacy_type = str(payload.get("legacy_event_type") or "").strip().lower()
    source = str(payload.get("source") or "").strip().lower()
    if stream_filter:
        stream_id = str(payload.get("stream_id") or "")
        if stream_id not in stream_filter and not (
            include_life_internal and _is_life_internal_payload(payload)
        ):
            return False
    type_candidates = {event_type, content_type, legacy_type}
    if type_filter and type_candidates.isdisjoint(type_filter):
        if not (
            match_minecraft
            and (
                source == "minecraft"
                or content_type.startswith("minecraft")
                or "minecraft" in content_type
            )
        ):
            return False
    elif match_minecraft and not type_filter:
        if not (
            source == "minecraft"
            or content_type.startswith("minecraft")
            or "minecraft" in content_type
        ):
            return False
    if exclude_types and not type_candidates.isdisjoint(exclude_types):
        return False
    if exclude_sources and source in exclude_sources:
        return False
    if channels and not _in_string_set(str(payload.get("channel") or ""), channels):
        return False
    if sources and source not in sources:
        return False
    if chat_types and not _in_string_set(str(payload.get("chat_type") or ""), chat_types):
        return False
    if content_types and content_type not in content_types:
        return False
    if instances and not _in_string_set(
        str(payload.get("source_instance_id") or ""), instances
    ):
        return False
    if person and person.lower() not in _person_haystack(payload).lower():
        return False
    if sender_ids and not _in_string_set(str(payload.get("sender_id") or ""), sender_ids):
        return False
    if senders and not _in_string_set(str(payload.get("sender") or ""), senders):
        return False
    if tool_names and not _in_string_set(str(payload.get("tool_name") or ""), tool_names):
        return False
    if tool_success is not None:
        value = payload.get("tool_success")
        if not isinstance(value, bool) or value is not tool_success:
            return False
    if call_id and str(payload.get("call_id") or "") != call_id:
        return False
    if parent_event_id and str(payload.get("parent_event_id") or "") != parent_event_id:
        return False
    if causation_ids and not _in_string_set(
        str(payload.get("causation_id") or ""), causation_ids
    ):
        return False
    if correlation_ids and not _in_string_set(
        str(payload.get("correlation_id") or ""), correlation_ids
    ):
        return False
    if heartbeat_run_ids and not _in_string_set(
        str(payload.get("heartbeat_run_id") or ""), heartbeat_run_ids
    ):
        return False
    if occurrence_ids and str(payload.get("occurrence_id") or "") not in occurrence_ids:
        return False
    if delivery_types and event_type not in delivery_types:
        return False
    return True


def _attached_store(service: LifeEngineService) -> Any | None:
    store = getattr(service, "_life_event_store", None)
    if store is not None:
        return store
    bus = getattr(service, "_event_bus", None)
    if bus is not None:
        return getattr(bus, "store", None)
    return None


async def _scan_ledger_payloads(
    store: Any,
    *,
    after_position: int,
    before_position: int | None,
    occurred_after: str | None,
    occurred_before: str | None,
    scan_limit: int,
    descending: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    scan_fn = getattr(store, "scan_window", None)
    if not callable(scan_fn):
        return [], {
            "scanned_events": 0,
            "scan_truncated": False,
            "default_window": "",
            "next_after_position": None,
            "next_before_position": None,
        }
    collected: list[dict[str, Any]] = []
    scanned = 0
    after = max(0, int(after_position))
    before = before_position
    truncated = False
    default_window = ""
    if (
        after == 0
        and before is None
        and not occurred_after
        and not occurred_before
    ):
        default_window = "ledger_tail"
    min_seen: int | None = None
    max_seen: int | None = None
    while scanned < scan_limit:
        page = min(_SCAN_PAGE, scan_limit - scanned)
        rows = await scan_fn(
            after_position=after,
            before_position=before,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            limit=page,
            descending=descending,
        )
        if not rows:
            break
        scanned += len(rows)
        positions = [int(getattr(row, "sequence", 0) or 0) for row in rows]
        min_seen = min(positions) if min_seen is None else min(min_seen, *positions)
        max_seen = max(positions) if max_seen is None else max(max_seen, *positions)
        for row in rows:
            collected.append(_event_to_payload(row, ledger_source="ledger"))
        if len(rows) < page:
            break
        if descending:
            before = min(positions)
        else:
            after = max(positions)
    else:
        truncated = True
    return collected, {
        "scanned_events": scanned,
        "scan_truncated": truncated,
        "default_window": default_window,
        "next_after_position": max_seen,
        "next_before_position": min_seen,
        "scanned_from_position": min_seen,
        "scanned_to_position": max_seen,
        "scanned_time_range": {
            "occurred_after": occurred_after or "",
            "occurred_before": occurred_before or "",
        },
    }


async def grep_life_events(
    *,
    query: str = "",
    use_regex: bool = False,
    case_insensitive: bool = True,
    stream_ids: list[str] | None = None,
    event_types: list[str] | None = None,
    fields: list[str] | None = None,
    include_pending: bool = True,
    include_life_internal: bool = False,
    limit: int = _DEFAULT_LIMIT,
    context_before: int = 1,
    context_after: int = 1,
    order: Literal["asc", "desc"] = "desc",
    after: str | None = None,
    before: str | None = None,
    last_hours: int | None = None,
    last_days: int | None = None,
    after_position: int | None = None,
    before_position: int | None = None,
    around_occurrence_id: str | None = None,
    source_instance_ids: list[str] | None = None,
    channels: list[str] | None = None,
    sources: list[str] | None = None,
    chat_types: list[str] | None = None,
    content_types: list[str] | None = None,
    kinds: list[str] | None = None,
    exclude_event_types: list[str] | None = None,
    exclude_sources: list[str] | None = None,
    person: str | None = None,
    sender_ids: list[str] | None = None,
    senders: list[str] | None = None,
    tool_names: list[str] | None = None,
    tool_success: bool | None = None,
    call_id: str | None = None,
    parent_event_id: str | None = None,
    causation_ids: list[str] | None = None,
    correlation_ids: list[str] | None = None,
    heartbeat_run_ids: list[str] | None = None,
    occurrence_ids: list[str] | None = None,
    delivery_status: str | None = None,
    scan_limit: int = _DEFAULT_SCAN_LIMIT,
) -> dict[str, Any]:
    """Search the Life Event ledger plus the derived pending/history buffer."""

    service = LifeEngineService.get_instance()
    if service is None:
        raise RuntimeError("life_engine 服务不可用")
    if (last_hours or last_days) and (after or before):
        raise ValueError("last_hours/last_days cannot be combined with after/before")

    field_names = [str(field or "").strip() for field in (fields or list(_TEXT_FIELDS))]
    field_names = [field for field in field_names if field] or list(_TEXT_FIELDS)
    pattern = _compile_pattern(
        query, use_regex=use_regex, case_insensitive=case_insensitive
    )
    stream_filter = {
        str(sid or "").strip() for sid in (stream_ids or []) if str(sid or "").strip()
    }
    type_filter, match_minecraft = _expand_event_types(event_types)
    kind_filter, kind_minecraft = _expand_event_types(kinds)
    content_filter = _normalize_strings(content_types)
    for item in kinds or []:
        text = str(item or "").strip().lower()
        if text and text not in _EVENT_TYPE_ALIASES and text != "minecraft":
            content_filter.add(text)
    if kind_filter:
        type_filter |= kind_filter
    match_minecraft = match_minecraft or kind_minecraft
    exclude_types, _ = _expand_event_types(exclude_event_types)
    resolved_limit = max(1, min(int(limit or _DEFAULT_LIMIT), _MAX_LIMIT))
    before_ctx = max(0, min(int(context_before or 0), 8))
    after_ctx = max(0, min(int(context_after or 0), 8))
    occurred_after = _parse_iso(after) or _relative_after(last_hours, last_days)
    occurred_before = _parse_iso(before)
    after_pos = max(0, int(after_position or 0))
    before_pos = int(before_position) if before_position is not None else None
    delivery = str(delivery_status or "").strip().lower()
    delivery_types: set[str] = set()
    if delivery:
        mapped = _DELIVERY_STATUS_TYPES.get(delivery)
        if mapped is None:
            raise ValueError("delivery_status must be failed, unknown, confirmed, or requested")
        delivery_types.add(mapped)

    store = _attached_store(service)
    around = str(around_occurrence_id or "").strip()
    if around:
        if around.startswith("life-event-occurrence:"):
            around = around.removeprefix("life-event-occurrence:").strip()
        digest_fn = getattr(store, "occurrence_digest", None) if store is not None else None
        if not callable(digest_fn):
            raise RuntimeError("around_occurrence_id requires a readable Life Event store")
        digest = await digest_fn(around)
        if digest is None:
            raise ValueError(f"occurrence not found: {around}")
        position = int(getattr(digest, "position", 0) or 0)
        radius = max(before_ctx, after_ctx, 8)
        after_pos = max(after_pos, position - radius - 1)
        upper = position + radius + 1
        before_pos = upper if before_pos is None else min(before_pos, upper)

    async with service._get_lock():
        runtime_events = list(getattr(service, "_event_history", []))
        if include_pending:
            runtime_events.extend(list(getattr(service, "_pending_events", [])))

    runtime_payloads = [
        _event_to_payload(event, ledger_source="pending" if include_pending and event in getattr(service, "_pending_events", []) else "history")
        for event in runtime_events
    ]
    # Recompute pending membership by identity; `in` on objects is identity-based
    # and fails after list copies.
    pending_ids = {
        str(getattr(event, "occurrence_id", "") or getattr(event, "event_id", "") or "")
        for event in list(getattr(service, "_pending_events", []))
    }
    for payload in runtime_payloads:
        identity = str(payload.get("occurrence_id") or payload.get("event_id") or "")
        payload["ledger_source"] = (
            "pending" if identity and identity in pending_ids else "history"
        )

    ledger_payloads: list[dict[str, Any]] = []
    scan_stats = {
        "scanned_events": 0,
        "scan_truncated": False,
        "default_window": "",
        "next_after_position": None,
        "next_before_position": None,
    }
    if store is not None:
        ledger_payloads, scan_stats = await _scan_ledger_payloads(
            store,
            after_position=after_pos,
            before_position=before_pos,
            occurred_after=occurred_after,
            occurred_before=occurred_before,
            scan_limit=max(1, int(scan_limit or _DEFAULT_SCAN_LIMIT)),
            descending=order != "asc",
        )

    def _payload_identities(payload: dict[str, Any]) -> set[str]:
        identities: set[str] = set()
        for key in ("occurrence_id", "event_id"):
            value = str(payload.get(key) or "").strip()
            if value:
                identities.add(value)
        return identities

    by_identity: dict[str, dict[str, Any]] = {}
    ordered: list[dict[str, Any]] = []
    for payload in [*ledger_payloads, *runtime_payloads]:
        identities = _payload_identities(payload)
        existing: dict[str, Any] | None = None
        for identity in identities:
            found = by_identity.get(identity)
            if found is not None:
                existing = found
                break
        if existing is not None:
            chosen = (
                payload if payload.get("ledger_source") == "ledger" else existing
            )
            if chosen is payload:
                for index, item in enumerate(ordered):
                    if item is existing:
                        ordered[index] = chosen
                        break
            for identity in identities | _payload_identities(chosen):
                by_identity[identity] = chosen
            continue
        ordered.append(payload)
        for identity in identities:
            by_identity[identity] = payload

    filter_kwargs = {
        "stream_filter": stream_filter,
        "include_life_internal": bool(include_life_internal),
        "type_filter": type_filter,
        "match_minecraft": match_minecraft,
        "exclude_types": exclude_types,
        "exclude_sources": _normalize_strings(exclude_sources),
        "channels": _normalize_strings(channels),
        "sources": _normalize_strings(sources),
        "chat_types": _normalize_strings(chat_types),
        "content_types": content_filter,
        "instances": _normalize_strings(source_instance_ids),
        "person": str(person or "").strip(),
        "sender_ids": _normalize_strings(sender_ids),
        "senders": _normalize_strings(senders),
        "tool_names": {
            str(name or "").strip().lower()
            for name in (tool_names or [])
            if str(name or "").strip()
        },
        "tool_success": tool_success,
        "call_id": str(call_id or "").strip(),
        "parent_event_id": str(parent_event_id or "").strip(),
        "causation_ids": _normalize_strings(causation_ids),
        "correlation_ids": _normalize_strings(correlation_ids),
        "heartbeat_run_ids": {
            str(item or "").strip()
            for item in (heartbeat_run_ids or [])
            if str(item or "").strip()
        },
        "occurrence_ids": {
            str(item or "").strip()
            for item in (occurrence_ids or [])
            if str(item or "").strip()
        },
        "delivery_types": delivery_types,
    }

    windowed: list[dict[str, Any]] = []
    for payload in ordered:
        if occurred_after and str(payload.get("timestamp") or "") < occurred_after:
            continue
        if occurred_before and str(payload.get("timestamp") or "") > occurred_before:
            continue
        if after_pos and int(payload.get("ingest_position") or 0) <= after_pos:
            if payload.get("ledger_source") != "pending":
                continue
        if before_pos is not None and int(payload.get("ingest_position") or 0) >= before_pos:
            continue
        if _matches_filters(payload, **filter_kwargs):
            windowed.append(payload)

    windowed.sort(key=lambda item: int(item.get("ingest_position") or 0))
    matches: list[dict[str, Any]] = []
    for index, payload in enumerate(windowed):
        if pattern is not None and not pattern.search(
            _haystack(payload, field_names)
        ):
            continue
        start = max(0, index - before_ctx)
        end = min(len(windowed), index + after_ctx + 1)
        matches.append(
            {
                "event": _excerpt_payload(payload),
                "context_before": [
                    _excerpt_payload(item) for item in windowed[start:index]
                ],
                "context_after": [
                    _excerpt_payload(item) for item in windowed[index + 1 : end]
                ],
            }
        )
    if order != "asc":
        matches.reverse()
    returned = matches[:resolved_limit]
    high_water = max(
        (int(payload.get("ingest_position") or 0) for payload in ordered),
        default=0,
    )
    return {
        "action": "grep_life_events",
        "query": str(query or ""),
        "use_regex": bool(use_regex),
        "case_insensitive": bool(case_insensitive),
        "scope": "filtered_streams" if stream_filter else "all_streams",
        "stream_ids": sorted(stream_filter),
        "event_types": sorted(type_filter),
        "fields": field_names,
        "include_pending": bool(include_pending),
        "include_life_internal": bool(include_life_internal),
        "order": order,
        "matches": returned,
        "stats": {
            "total_events": len(ordered),
            "scanned_events": int(scan_stats.get("scanned_events") or len(ordered)),
            "matched_events": len(matches),
            "returned_matches": len(returned),
            "truncated": len(matches) > len(returned),
            "scan_truncated": bool(scan_stats.get("scan_truncated")),
            "default_window": str(scan_stats.get("default_window") or ""),
        },
        "scan": {
            "next_after_position": scan_stats.get("next_after_position"),
            "next_before_position": scan_stats.get("next_before_position"),
            "scanned_from_position": scan_stats.get("scanned_from_position"),
            "scanned_to_position": scan_stats.get("scanned_to_position"),
            "scanned_time_range": scan_stats.get("scanned_time_range") or {},
        },
        "source_frontier": {
            "event_high_water": high_water,
            "event_count": len(ordered),
            "events_sha256": sha256_json(
                [payload.get("occurrence_id") for payload in ordered]
            ),
        },
    }


class LifeEngineGrepEventsTool(BaseTool):
    """Search the authoritative Life Event ledger with optional filters."""

    tool_name: str = "nucleus_grep_events"
    tool_description: str = (
        "搜索权威 Life Event 账本（不是只搜近期内存投影）。命中只返回节选；"
        "全文用 nucleus_read_event(occurrence_id) 续读。\n"
        "query 可空：只靠过滤浏览。条件全部可选、可组合，按时间序返回，不按重要性排序。\n"
        "时间：after/before（ISO）、last_hours/last_days、after_position/before_position、"
        "around_occurrence_id。扫不全时 stats.scan_truncated=true，用 scan.next_before_position 继续。\n"
        "窗口：source_instance_ids、stream_ids、cross_stream、channels、sources、chat_types。\n"
        "种类：event_types 接受原名或别名 thought/tool/chat/minecraft；"
        "content_types/kinds；exclude_event_types/exclude_sources。\n"
        "人：person（sender/sender_id/canonical_person_key/actor_id）、sender_ids、senders。\n"
        "工具：tool_names、tool_success、call_id、parent_event_id。\n"
        "因果：causation_ids、correlation_ids、heartbeat_run_ids、occurrence_ids。\n"
        "投递：delivery_status=failed/unknown/confirmed/requested。\n"
        "查找文件用 nucleus_grep_file；TODO 用 nucleus_todo。这不是向量检索。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        query: Annotated[str, "关键词或正则；可空，只靠过滤浏览"] = "",
        use_regex: Annotated[bool, "是否按正则表达式匹配 query"] = False,
        case_insensitive: Annotated[bool, "匹配时是否忽略大小写"] = True,
        cross_stream: Annotated[bool, "是否跨所有聊天流搜索；默认 false"] = False,
        stream_ids: Annotated[list[str] | None, "限定 stream_id"] = None,
        event_types: Annotated[
            list[str] | None,
            "事件类型或别名 thought/tool/chat/minecraft",
        ] = None,
        fields: Annotated[list[str] | None, "搜索字段；空则常用文本字段"] = None,
        include_pending: Annotated[bool, "是否包含尚未 checkpoint 的 pending"] = True,
        include_life_internal: Annotated[bool | None, "限定 stream 时是否仍含 life 内部事件"] = None,
        limit: Annotated[int, "最大返回命中数"] = _DEFAULT_LIMIT,
        context_before: Annotated[int, "每条命中前带几条相邻事件"] = 1,
        context_after: Annotated[int, "每条命中后带几条相邻事件"] = 1,
        order: Annotated[Literal["asc", "desc"], "返回顺序：asc/desc"] = "desc",
        after: Annotated[str | None, "只看该 ISO 时间之后"] = None,
        before: Annotated[str | None, "只看该 ISO 时间之前"] = None,
        last_hours: Annotated[int | None, "最近 N 小时"] = None,
        last_days: Annotated[int | None, "最近 N 天"] = None,
        after_position: Annotated[int | None, "账本 ingest_position 之后"] = None,
        before_position: Annotated[int | None, "账本 ingest_position 之前"] = None,
        around_occurrence_id: Annotated[str | None, "先定位该 occurrence 再取邻域"] = None,
        source_instance_ids: Annotated[list[str] | None, "意识实例 id"] = None,
        channels: Annotated[list[str] | None, "chat/life/tool/agent/proactive/system"] = None,
        sources: Annotated[list[str] | None, "qq/feishu/minecraft/life_engine 等"] = None,
        chat_types: Annotated[list[str] | None, "private/group/minecraft"] = None,
        content_types: Annotated[list[str] | None, "精确 content_type/kind"] = None,
        kinds: Annotated[list[str] | None, "content_type 别名，同 content_types"] = None,
        exclude_event_types: Annotated[list[str] | None, "排除的类型或别名"] = None,
        exclude_sources: Annotated[list[str] | None, "排除的 source"] = None,
        person: Annotated[str | None, "匹配 sender/sender_id/canonical_person_key/actor_id"] = None,
        sender_ids: Annotated[list[str] | None, "精确 sender_id"] = None,
        senders: Annotated[list[str] | None, "精确 sender 显示名"] = None,
        tool_names: Annotated[list[str] | None, "工具名"] = None,
        tool_success: Annotated[bool | None, "工具成败；省略表示不限"] = None,
        call_id: Annotated[str | None, "工具 call_id"] = None,
        parent_event_id: Annotated[str | None, "父事件 id"] = None,
        causation_ids: Annotated[list[str] | None, "causation_id"] = None,
        correlation_ids: Annotated[list[str] | None, "correlation_id"] = None,
        heartbeat_run_ids: Annotated[list[str] | None, "heartbeat_run_id"] = None,
        occurrence_ids: Annotated[list[str] | None, "精确 occurrence 集合"] = None,
        delivery_status: Annotated[
            str | None,
            "chat 投递：failed/unknown/confirmed/requested",
        ] = None,
        continuation: Annotated[
            str,
            "Optional continuation returned by the previous event grep page",
        ] = "",
        max_bytes: Annotated[
            int | None,
            "Optional result byte budget; the task hard cap still applies",
        ] = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        resolved_stream_ids = [
            str(stream_id or "").strip()
            for stream_id in (stream_ids or [])
            if str(stream_id or "").strip()
        ]
        chat_stream = getattr(self, "chat_stream", None)
        current_stream_id = str(
            getattr(chat_stream, "stream_id", "") or ""
        ).strip()
        if not cross_stream and not resolved_stream_ids and current_stream_id:
            resolved_stream_ids = [current_stream_id]

        include_internal = (
            True
            if include_life_internal is None and not current_stream_id
            else (
                bool(current_stream_id)
                if include_life_internal is None
                else include_life_internal
            )
        )
        try:
            result = await grep_life_events(
                query=query,
                use_regex=use_regex,
                case_insensitive=case_insensitive,
                stream_ids=[] if cross_stream else resolved_stream_ids,
                event_types=event_types,
                fields=fields,
                include_pending=include_pending,
                include_life_internal=bool(include_internal and not cross_stream)
                if current_stream_id
                else bool(include_internal),
                limit=limit,
                context_before=context_before,
                context_after=context_after,
                order=order,
                after=after,
                before=before,
                last_hours=last_hours,
                last_days=last_days,
                after_position=after_position,
                before_position=before_position,
                around_occurrence_id=around_occurrence_id,
                source_instance_ids=source_instance_ids,
                channels=channels,
                sources=sources,
                chat_types=chat_types,
                content_types=content_types,
                kinds=kinds,
                exclude_event_types=exclude_event_types,
                exclude_sources=exclude_sources,
                person=person,
                sender_ids=sender_ids,
                senders=senders,
                tool_names=tool_names,
                tool_success=tool_success,
                call_id=call_id,
                parent_event_id=parent_event_id,
                causation_ids=causation_ids,
                correlation_ids=correlation_ids,
                heartbeat_run_ids=heartbeat_run_ids,
                occurrence_ids=occurrence_ids,
                delivery_status=delivery_status,
            )
            source_items = list(result.get("matches") or [])
            item_refs = []
            for item in source_items:
                item_hash = sha256_json(item)
                event = item.get("event") or {}
                event_id = str(
                    event.get("occurrence_id") or event.get("event_id") or ""
                ).strip()
                item_refs.append(
                    f"life-event-match:{event_id or 'unknown'}:sha256:{item_hash}"
                )
            raw_stats = dict(result.get("stats") or {})
            raw_stats["retrieved_matches"] = len(source_items)
            raw_stats.pop("returned_matches", None)
            projected = project_bounded_items(
                projection_name="life-event-grep",
                task_name=getattr(self, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={
                    "query": str(query),
                    "use_regex": bool(use_regex),
                    "case_insensitive": bool(case_insensitive),
                    "cross_stream": bool(cross_stream),
                    "stream_ids": sorted(resolved_stream_ids),
                    "event_types": sorted(
                        str(value or "").strip() for value in (event_types or [])
                    ),
                    "fields": [
                        str(value or "").strip() for value in (fields or [])
                    ],
                    "include_pending": bool(include_pending),
                    "include_life_internal": bool(include_internal),
                    "limit": int(limit),
                    "context_before": int(context_before),
                    "context_after": int(context_after),
                    "order": str(order),
                    "after": str(after or ""),
                    "before": str(before or ""),
                    "last_hours": last_hours,
                    "last_days": last_days,
                    "after_position": after_position,
                    "before_position": before_position,
                    "around_occurrence_id": str(around_occurrence_id or ""),
                    "source_instance_ids": list(source_instance_ids or []),
                    "channels": list(channels or []),
                    "sources": list(sources or []),
                    "person": str(person or ""),
                    "tool_names": list(tool_names or []),
                    "tool_success": tool_success,
                    "delivery_status": str(delivery_status or ""),
                },
                frontier=result.get("source_frontier") or {},
                base_payload={
                    **{
                        key: value
                        for key, value in result.items()
                        if key not in {"matches", "stats"}
                    },
                    "stats": raw_stats,
                },
                items_key="matches",
                items=source_items,
                item_refs=item_refs,
                continuation=continuation,
                tolerate_frontier_change=True,
            )
            if len(str(projected).encode("utf-8")) > projected["budget_bytes"]:
                raise ValueError("event grep projection exceeded its byte budget")
            return True, projected
        except re.error as exc:
            return False, f"正则表达式错误: {exc}"
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"搜索 life 事件流失败: {type(exc).__name__}")
            return False, f"搜索事件流失败: {type(exc).__name__}: {exc}"


async def _read_authoritative_event(
    service: LifeEngineService,
    occurrence_id: str,
) -> tuple[Any | None, dict[str, Any]]:
    """Read one immutable occurrence from either local or selected storage."""

    identity = str(occurrence_id or "").strip()
    if identity.startswith("life-event-occurrence:"):
        identity = identity.removeprefix("life-event-occurrence:").strip()
    if not identity:
        raise ValueError("occurrence_id 不能为空")
    store = service._get_life_event_store()
    get_by_event_id = getattr(store, "get_by_event_id", None)
    if callable(get_by_event_id):
        event = await get_by_event_id(identity)
        if event is None:
            return None, {"occurrence_id": identity}
        return event, {
            "occurrence_id": str(getattr(event, "occurrence_id", "") or identity),
            "position": int(getattr(event, "sequence", 0) or 0),
            "content_sha256": sha256_json(
                {"content": _authoritative_event_content(event)}
            ),
        }

    digest_lookup = getattr(store, "occurrence_digest", None)
    read_since = getattr(store, "read_since", None)
    if not callable(digest_lookup) or not callable(read_since):
        raise RuntimeError("authoritative Life Event store is not readable")
    digest = await digest_lookup(identity)
    if digest is None:
        return None, {"occurrence_id": identity}
    position = int(getattr(digest, "position", 0) or 0)
    rows = await read_since(max(0, position - 1), limit=1)
    event = rows[0] if rows else None
    if (
        event is None
        or int(getattr(event, "sequence", 0) or 0) != position
        or str(getattr(event, "occurrence_id", "") or "") != identity
    ):
        raise RuntimeError("authoritative Life Event occurrence read mismatch")
    return event, {
        "occurrence_id": identity,
        "position": position,
        "payload_hash": str(getattr(digest, "payload_hash", "") or ""),
    }


class LifeEngineReadEventTool(BaseTool):
    """Read one complete immutable Life Event through stable UTF-8 chunks."""

    tool_name: str = "nucleus_read_event"
    tool_description: str = (
        "按潜意识投影给出的 occurrence_id 精确读取一条完整 Life Event。"
        "当事件投影显示 excerpt_ref、content_ref 或原文过长时使用；"
        "continuation 用于继续读取同一不可变事件，不能跨事件复用。"
    )
    chatter_allow: list[str] = ["life_engine_internal", "life_chatter"]

    async def execute(
        self,
        occurrence_id: Annotated[
            str,
            "不可变 Life Event occurrence_id；可从 life-event-occurrence: 引用中取得",
        ],
        continuation: Annotated[
            str,
            "上一页返回的 continuation；第一页留空",
        ] = "",
        max_bytes: Annotated[
            int | None,
            "可选结果字节预算；不能突破当前任务硬上限",
        ] = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        try:
            service = LifeEngineService.get_instance()
            if service is None:
                raise RuntimeError("life_engine 服务不可用")
            event, frontier = await _read_authoritative_event(
                service,
                occurrence_id,
            )
            if event is None:
                return False, "未找到对应的 Life Event occurrence"
            identity = str(getattr(event, "occurrence_id", "") or occurrence_id)
            content = _authoritative_event_content(event)
            projected = project_bounded_text(
                projection_name="life-event-authority-read",
                task_name=getattr(self, "_runtime_task_name", ""),
                requested_max_bytes=max_bytes,
                binding={"occurrence_id": identity},
                frontier=frontier,
                base_payload={
                    "action": "read_life_event",
                    "occurrence_id": identity,
                    "event_id": str(getattr(event, "event_id", "") or ""),
                    "event_type": str(getattr(event, "event_type", "") or ""),
                    "channel": str(getattr(event, "channel", "") or ""),
                    "source": str(getattr(event, "source", "") or ""),
                    "stream_id": str(getattr(event, "stream_id", "") or ""),
                    "source_instance_id": str(
                        getattr(event, "source_instance_id", "") or ""
                    ),
                    "causation_id": str(
                        getattr(event, "causation_id", "") or ""
                    ),
                    "correlation_id": str(
                        getattr(event, "correlation_id", "") or ""
                    ),
                },
                content=content,
                content_ref=(
                    str(getattr(event, "content_ref", "") or "").strip()
                    or f"life-event-occurrence:{identity}"
                ),
                continuation=continuation,
            )
            return True, projected
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "读取 Life Event occurrence 失败: "
                f"error_type={type(exc).__name__}"
            )
            return False, f"读取 Life Event 失败: {type(exc).__name__}"


EVENT_GREP_TOOLS = [
    LifeEngineGrepEventsTool,
    LifeEngineReadEventTool,
]
