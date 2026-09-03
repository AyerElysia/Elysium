"""Independent append-only rolling context for the subconscious heartbeat.

Chatter's rolling snapshot, archive namespace and live runtime are never
read or written here.  New life-domain events are projected as visible
text; protocol envelopes stay in the authoritative Life Event ledger.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from src.kernel.llm import ROLE, LLMPayload, ReasoningText, Text, ToolCall, ToolResult
from src.kernel.storage import canonical_json_sha256

from .event_builder import EventType, LifeEngineEvent

HEARTBEAT_ROLLING_NAMESPACE = "life_heartbeat.rolling_context"
HEARTBEAT_ROLLING_STATE_KEY = "subconscious"
HEARTBEAT_ROLLING_SNAPSHOT_VERSION = 1
HEARTBEAT_ROLLING_FILENAME = "life_heartbeat_rolling_context.json"

_VISIBLE_JSON_KEYS = (
    "assistant_message",
    "thought",
    "content",
    "text",
    "statement",
    "continuity_text",
)


def format_visible_event(event: LifeEngineEvent) -> str:
    """Render one life-domain event as first-person-readable text.

    Infrastructure protocol JSON is not copied into the rolling window.
    """

    event_type = event.event_type
    if event_type == EventType.SUMMARY:
        return ""
    content = str(event.content or "").strip()
    if event_type == EventType.MESSAGE:
        sender = str(event.sender or event.source or "").strip()
        body = content or _visible_from_raw(event.raw_content)
        if not body:
            return ""
        return f"{sender}: {body}" if sender else body
    if event_type == EventType.HEARTBEAT:
        return content or _visible_from_raw(event.raw_content)
    if event_type == EventType.CONSCIOUS_ACTIVITY:
        extracted = _visible_from_raw(event.raw_content) or content
        return extracted
    if event_type == EventType.TOOL_CALL:
        name = str(event.tool_name or "tool").strip() or "tool"
        args = event.tool_args if isinstance(event.tool_args, dict) else {}
        rendered_args = _short_args(args)
        return f"{name}({rendered_args})" if rendered_args else name
    if event_type == EventType.TOOL_RESULT:
        name = str(event.tool_name or "tool").strip() or "tool"
        body = content or _visible_from_raw(event.raw_content)
        status = "ok" if event.tool_success is not False else "fail"
        if body:
            return f"{name} [{status}] {body}"
        return f"{name} [{status}]"
    if event_type == EventType.AGENT_RESULT:
        name = str(event.tool_name or "agent").strip() or "agent"
        body = content or _visible_from_raw(event.raw_content)
        return f"{name}: {body}" if body else name
    return content


def format_new_events_text(events: Sequence[LifeEngineEvent]) -> str:
    """Join newly selected events in sequence as one rolling USER body."""

    lines: list[str] = []
    for event in events:
        text = format_visible_event(event)
        if text:
            lines.append(text)
    return "\n".join(lines)


def rolling_payloads_only(payloads: Sequence[LLMPayload]) -> list[LLMPayload]:
    """Drop pinned SYSTEM/TOOL prefix; rolling never owns those bytes."""

    return [
        payload
        for payload in payloads
        if isinstance(payload, LLMPayload)
        and getattr(payload, "role", None) not in {ROLE.SYSTEM, ROLE.TOOL}
    ]


def serialize_rolling_payloads(payloads: Sequence[LLMPayload]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for payload in rolling_payloads_only(payloads):
        serialized = _serialize_payload(payload)
        if serialized is not None:
            items.append(serialized)
    return items


def deserialize_rolling_payloads(raw: Any) -> list[LLMPayload]:
    if not isinstance(raw, dict):
        raise RuntimeError("HeartbeatRollingSnapshotNotObject")
    version = raw.get("version", 1)
    if version != HEARTBEAT_ROLLING_SNAPSHOT_VERSION:
        raise RuntimeError(f"HeartbeatRollingSnapshotVersionUnsupported:{version}")
    payload_items = raw.get("payloads")
    if not isinstance(payload_items, list):
        raise RuntimeError("HeartbeatRollingPayloadsNotList")
    expected = raw.get("payload_digest")
    actual = canonical_json_sha256(payload_items)
    if not isinstance(expected, str) or expected != actual:
        raise RuntimeError("HeartbeatRollingPayloadDigestMismatch")
    payloads: list[LLMPayload] = []
    for item in payload_items:
        payload = _deserialize_payload(item)
        if payload is not None:
            payloads.append(payload)
    return payloads


def snapshot_dict(payloads: Sequence[LLMPayload]) -> dict[str, Any]:
    serialized = serialize_rolling_payloads(payloads)
    return {
        "version": HEARTBEAT_ROLLING_SNAPSHOT_VERSION,
        "runtime_key": HEARTBEAT_ROLLING_NAMESPACE,
        "payload_digest": canonical_json_sha256(serialized),
        "payloads": serialized,
    }


async def load_heartbeat_rolling(
    *,
    service: Any | None,
    workspace_path: str,
) -> list[LLMPayload]:
    store = _runtime_store(service)
    if store is not None:
        record = await store.get_state(
            HEARTBEAT_ROLLING_NAMESPACE,
            HEARTBEAT_ROLLING_STATE_KEY,
        )
        if record is None:
            return []
        return deserialize_rolling_payloads(record.payload)

    path = _local_snapshot_path(workspace_path)
    if not path.exists():
        return []
    try:
        raw = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - derived snapshot fails closed to empty
        raise RuntimeError("HeartbeatRollingSnapshotUnreadable") from exc
    return deserialize_rolling_payloads(raw)


async def save_heartbeat_rolling(
    payloads: Sequence[LLMPayload],
    *,
    service: Any | None,
    workspace_path: str,
) -> None:
    data = snapshot_dict(payloads)
    store = _runtime_store(service)
    if store is not None:
        latest = await store.get_state(
            HEARTBEAT_ROLLING_NAMESPACE,
            HEARTBEAT_ROLLING_STATE_KEY,
        )
        expected_revision = int(latest.revision) if latest is not None else 0
        await store.put_state(
            namespace=HEARTBEAT_ROLLING_NAMESPACE,
            state_key=HEARTBEAT_ROLLING_STATE_KEY,
            expected_revision=expected_revision,
            schema_version=HEARTBEAT_ROLLING_SNAPSHOT_VERSION,
            payload=data,
        )
        return

    path = _local_snapshot_path(workspace_path)
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}-{uuid.uuid4().hex}")
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)
    try:
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        await asyncio.to_thread(tmp.write_text, text, encoding="utf-8")
        await asyncio.to_thread(os.replace, tmp, path)
    finally:
        try:
            await asyncio.to_thread(tmp.unlink, missing_ok=True)
        except OSError:
            pass


def estimate_payload_chars(payloads: Sequence[LLMPayload]) -> int:
    try:
        return len(
            json.dumps(
                snapshot_dict(payloads),
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
        )
    except Exception:  # noqa: BLE001
        return sum(len(str(payload)) for payload in payloads)


def _runtime_store(service: Any | None) -> Any | None:
    if service is None:
        return None
    getter = getattr(service, "runtime_state_store", None)
    if not callable(getter):
        return None
    return getter()


def _local_snapshot_path(workspace_path: str) -> Path:
    workspace = str(workspace_path or "").strip()
    if not workspace:
        raise RuntimeError("HeartbeatRollingWorkspaceMissing")
    return Path(workspace).expanduser() / "runtime" / HEARTBEAT_ROLLING_FILENAME


def _visible_from_raw(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if isinstance(parsed, dict):
        for key in _VISIBLE_JSON_KEYS:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        payload = parsed.get("payload")
        if isinstance(payload, dict):
            for key in _VISIBLE_JSON_KEYS:
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _short_args(args: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in list(args.items())[:6]:
        rendered = str(value)
        if len(rendered) > 80:
            rendered = rendered[:77] + "..."
        parts.append(f"{key}={rendered}")
    return ", ".join(parts)


def _serialize_payload(payload: LLMPayload) -> dict[str, Any] | None:
    role = getattr(getattr(payload, "role", None), "value", None)
    if not isinstance(role, str) or not role:
        return None
    content: list[dict[str, Any]] = []
    for part in list(payload.content or []):
        item = _serialize_part(part)
        if item is not None:
            content.append(item)
    if not content:
        return None
    return {"role": role, "content": content}


def _serialize_part(part: object) -> dict[str, Any] | None:
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
            "args": part.args if isinstance(part.args, dict) else {},
        }
    if isinstance(part, ToolResult):
        return {
            "type": "tool_result",
            "call_id": str(part.call_id or ""),
            "name": str(part.name or ""),
            "value": part.value,
        }
    return None


def _deserialize_payload(data: Any) -> LLMPayload | None:
    if not isinstance(data, dict):
        return None
    try:
        role = ROLE(str(data.get("role") or ""))
    except ValueError:
        return None
    if role in {ROLE.SYSTEM, ROLE.TOOL}:
        return None
    content = [
        item
        for item in (_deserialize_part(part) for part in list(data.get("content") or []))
        if item is not None
    ]
    if not content:
        return None
    return LLMPayload(role, content)  # type: ignore[arg-type]


def _deserialize_part(part: Any) -> object | None:
    if not isinstance(part, dict):
        return None
    kind = str(part.get("type") or "")
    if kind == "text":
        return Text(str(part.get("text") or ""))
    if kind == "reasoning_text":
        return ReasoningText(
            str(part.get("text") or ""),
            signature=str(part.get("signature") or ""),
            redacted_data=str(part.get("redacted_data") or ""),
        )
    if kind == "tool_call":
        args = part.get("args")
        return ToolCall(
            id=str(part.get("call_id") or ""),
            name=str(part.get("name") or ""),
            args=args if isinstance(args, dict) else {},
        )
    if kind == "tool_result":
        return ToolResult(
            value=part.get("value"),
            call_id=str(part.get("call_id") or ""),
            name=str(part.get("name") or ""),
        )
    return None


def iter_selected_events(
    events: Iterable[LifeEngineEvent],
    selected_ids: Sequence[str],
) -> list[LifeEngineEvent]:
    wanted = {str(item) for item in selected_ids if str(item)}
    if not wanted:
        return []
    return [event for event in events if str(event.event_id) in wanted]
