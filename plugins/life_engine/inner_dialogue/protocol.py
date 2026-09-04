"""Structured inner-dialogue sink/return protocol.

Life Events remain the append-only authority. This ledger is a rebuildable
projection so open receipts survive heartbeat cursor compacting.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any

INNER_DIALOGUE_KIND = "inner_dialogue"
INNER_DIALOGUE_RETURN_KIND = "inner_dialogue_return"
INNER_DIALOGUE_RETURN_DELIVERY_KIND = "inner_dialogue_return_delivery"
INNER_DIALOGUE_OPEN_LIMIT = 32
INNER_RETURN_SENDER_ID = "life_engine_inner_return"

_HEADER_RECEIPT_RE = re.compile(r"receipt=([^\s|\]]+)")
_HEADER_EXPECT_RE = re.compile(r"expect_surface=(yes|no)")
_HEADER_MODE_RE = re.compile(r"mode=([a-z]+)")


class InnerDialogueOpenLimitExceeded(RuntimeError):
    """Raised when another expect_surface sink would exceed the engineering cap."""

    def __init__(self, *, open_count: int, limit: int = INNER_DIALOGUE_OPEN_LIMIT) -> None:
        self.open_count = int(open_count)
        self.limit = int(limit)
        super().__init__(
            f"InnerDialogueOpenLimitExceeded: open={self.open_count} limit={self.limit}"
        )


class InnerDialogueReturnBlocked(RuntimeError):
    """Raised when a return cannot target the originating expression window."""

    def __init__(self, reason: str, *, receipt_id: str = "") -> None:
        self.reason = str(reason or "blocked")
        self.receipt_id = str(receipt_id or "")
        super().__init__(
            f"InnerDialogueReturnBlocked: {self.reason}"
            + (f" receipt={self.receipt_id}" if self.receipt_id else "")
        )


class InnerDialogueReturnRequiresHeartbeat(PermissionError):
    """Expression windows cannot close their own unsunk inner dialogue."""

    def __init__(self) -> None:
        super().__init__("InnerDialogueReturnRequiresHeartbeat")


class InnerDialogueConflict(RuntimeError):
    """Same return occurrence reused with different statement content."""

    def __init__(self, *, receipt_id: str, occurrence_id: str) -> None:
        self.receipt_id = str(receipt_id or "")
        self.occurrence_id = str(occurrence_id or "")
        super().__init__(
            "InnerDialogueConflict: "
            f"receipt={self.receipt_id} occurrence={self.occurrence_id}"
        )


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(payload),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def dump_inner_dialogue_payload(payload: Mapping[str, Any]) -> str:
    """Serialize one structured inner-dialogue payload."""

    return _canonical_json(payload)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _utf8_bytes(value: str) -> int:
    return len(str(value or "").encode("utf-8"))


def parse_inner_dialogue_payload(event: Any) -> dict[str, Any] | None:
    """Read structured inner-dialogue fields; never invent cognitive meaning."""

    content_type = str(getattr(event, "content_type", "") or "").strip().lower()
    if content_type not in {
        INNER_DIALOGUE_KIND,
        INNER_DIALOGUE_RETURN_KIND,
        INNER_DIALOGUE_RETURN_DELIVERY_KIND,
    }:
        return None

    raw = getattr(event, "raw_content", None)
    parsed: dict[str, Any] | None = None
    if isinstance(raw, dict):
        parsed = dict(raw)
    elif isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            loaded = None
        if isinstance(loaded, dict):
            parsed = dict(loaded)

    if parsed is None:
        parsed = _parse_legacy_inner_dialogue_prose(event, content_type)

    parsed["kind"] = str(parsed.get("kind") or content_type).strip() or content_type
    receipt_id = str(
        parsed.get("receipt_id")
        or getattr(event, "correlation_id", "")
        or ""
    ).strip()
    if not receipt_id:
        return None
    parsed["receipt_id"] = receipt_id
    parsed["event_id"] = str(parsed.get("event_id") or getattr(event, "event_id", "") or "")
    parsed["sequence"] = int(
        parsed.get("sequence") or getattr(event, "sequence", 0) or 0
    )
    parsed["timestamp"] = str(
        parsed.get("timestamp") or getattr(event, "timestamp", "") or ""
    )
    parsed["stream_id"] = str(
        parsed.get("stream_id") or getattr(event, "stream_id", "") or ""
    ).strip()
    parsed["source_instance_id"] = str(
        parsed.get("source_instance_id")
        or getattr(event, "source_instance_id", "")
        or ""
    ).strip()
    parsed["occurrence_id"] = str(
        parsed.get("occurrence_id")
        or parsed.get("return_occurrence_id")
        or getattr(event, "occurrence_id", "")
        or ""
    ).strip()
    return parsed


def _parse_legacy_inner_dialogue_prose(
    event: Any,
    content_type: str,
) -> dict[str, Any]:
    """Best-effort rebuild from historical header prose; not used for new events."""

    content = str(getattr(event, "content", "") or "")
    receipt_match = _HEADER_RECEIPT_RE.search(content)
    expect_match = _HEADER_EXPECT_RE.search(content)
    mode_match = _HEADER_MODE_RE.search(content)
    thought = content
    marker = "]\n"
    if marker in content:
        thought = content.split(marker, 1)[1]
    return {
        "kind": content_type,
        "receipt_id": str(receipt_match.group(1) if receipt_match else "").strip(),
        "expect_surface": bool(expect_match and expect_match.group(1) == "yes"),
        "mode": str(mode_match.group(1) if mode_match else "reflect"),
        "thought": thought,
        "stream_id": str(getattr(event, "stream_id", "") or "").strip(),
        "source_instance_id": str(getattr(event, "source_instance_id", "") or "").strip(),
        "event_id": str(getattr(event, "event_id", "") or ""),
        "legacy_prose": True,
    }


def inner_dialogue_summary(record: InnerDialogueRecord) -> dict[str, Any]:
    """Content-neutral open-set index; full thought is a separate read."""

    blocked = ""
    if record.expect_surface and not record.stream_id and record.status == "open":
        blocked = "missing_stream"
    return {
        "receipt_id": record.receipt_id,
        "status": record.status,
        "expect_surface": record.expect_surface,
        "stream_id": record.stream_id,
        "source_instance_id": record.source_instance_id,
        "mode": record.mode,
        "sunk_at": record.sunk_at,
        "event_id": record.sink_event_id,
        "thought_bytes": record.thought_bytes,
        "thought_sha256": record.thought_sha256,
        "return_blocked": blocked,
        "wake_pending": record.wake_pending,
        "wake_delivered": record.wake_delivered,
        "return_occurrence_id": record.return_occurrence_id,
    }


@dataclass(slots=True)
class InnerDialogueRecord:
    """One inner-dialogue receipt and its return/delivery projection."""

    receipt_id: str
    sink_event_id: str = ""
    sequence: int = 0
    sunk_at: str = ""
    thought: str = ""
    thought_sha256: str = ""
    thought_bytes: int = 0
    mode: str = "reflect"
    expect_surface: bool = False
    stream_id: str = ""
    source_instance_id: str = ""
    platform: str = ""
    chat_type: str = ""
    status: str = "internal"
    return_occurrence_id: str = ""
    return_event_id: str = ""
    return_statement: str = ""
    return_statement_sha256: str = ""
    return_actor: str = ""
    wake_pending: bool = False
    wake_delivered: bool = False
    delivery_event_id: str = ""
    trigger_message_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "sink_event_id": self.sink_event_id,
            "sequence": self.sequence,
            "sunk_at": self.sunk_at,
            "thought": self.thought,
            "thought_sha256": self.thought_sha256,
            "thought_bytes": self.thought_bytes,
            "mode": self.mode,
            "expect_surface": self.expect_surface,
            "stream_id": self.stream_id,
            "source_instance_id": self.source_instance_id,
            "platform": self.platform,
            "chat_type": self.chat_type,
            "status": self.status,
            "return_occurrence_id": self.return_occurrence_id,
            "return_event_id": self.return_event_id,
            "return_statement": self.return_statement,
            "return_statement_sha256": self.return_statement_sha256,
            "return_actor": self.return_actor,
            "wake_pending": self.wake_pending,
            "wake_delivered": self.wake_delivered,
            "delivery_event_id": self.delivery_event_id,
            "trigger_message_id": self.trigger_message_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> InnerDialogueRecord:
        thought = str(data.get("thought") or "")
        return cls(
            receipt_id=str(data.get("receipt_id") or "").strip(),
            sink_event_id=str(data.get("sink_event_id") or ""),
            sequence=int(data.get("sequence") or 0),
            sunk_at=str(data.get("sunk_at") or ""),
            thought=thought,
            thought_sha256=str(data.get("thought_sha256") or _sha256_text(thought)),
            thought_bytes=int(data.get("thought_bytes") or _utf8_bytes(thought)),
            mode=str(data.get("mode") or "reflect"),
            expect_surface=bool(data.get("expect_surface")),
            stream_id=str(data.get("stream_id") or "").strip(),
            source_instance_id=str(data.get("source_instance_id") or "").strip(),
            platform=str(data.get("platform") or ""),
            chat_type=str(data.get("chat_type") or ""),
            status=str(data.get("status") or "internal"),
            return_occurrence_id=str(data.get("return_occurrence_id") or ""),
            return_event_id=str(data.get("return_event_id") or ""),
            return_statement=str(data.get("return_statement") or ""),
            return_statement_sha256=str(data.get("return_statement_sha256") or ""),
            return_actor=str(data.get("return_actor") or ""),
            wake_pending=bool(data.get("wake_pending")),
            wake_delivered=bool(data.get("wake_delivered")),
            delivery_event_id=str(data.get("delivery_event_id") or ""),
            trigger_message_id=str(data.get("trigger_message_id") or ""),
        )


@dataclass
class InnerDialogueLedger:
    """Rebuildable open-set / pending-wake projection."""

    records: dict[str, InnerDialogueRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": {
                receipt_id: record.to_dict()
                for receipt_id, record in self.records.items()
            }
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> InnerDialogueLedger:
        raw = data.get("records") if isinstance(data, Mapping) else None
        records: dict[str, InnerDialogueRecord] = {}
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                if not isinstance(value, Mapping):
                    continue
                record = InnerDialogueRecord.from_dict(value)
                receipt_id = record.receipt_id or str(key).strip()
                if receipt_id:
                    records[receipt_id] = record
        return cls(records=records)

    def open_count(self) -> int:
        return sum(1 for record in self.records.values() if record.status == "open")

    def open_records(self) -> tuple[InnerDialogueRecord, ...]:
        items = [
            record
            for record in self.records.values()
            if record.status == "open"
        ]
        items.sort(key=lambda item: (int(item.sequence or 0), item.receipt_id))
        return tuple(items)

    def pending_wakes(self) -> tuple[InnerDialogueRecord, ...]:
        items = [
            record
            for record in self.records.values()
            if record.wake_pending and not record.wake_delivered
        ]
        items.sort(key=lambda item: (int(item.sequence or 0), item.receipt_id))
        return tuple(items)

    def get(self, receipt_id: str) -> InnerDialogueRecord | None:
        identity = str(receipt_id or "").strip()
        if not identity:
            return None
        return self.records.get(identity)

    def apply_events(self, events: list[Any] | tuple[Any, ...]) -> None:
        ordered = sorted(
            events,
            key=lambda event: (
                int(getattr(event, "sequence", 0) or 0),
                str(getattr(event, "event_id", "") or ""),
            ),
        )
        for event in ordered:
            self.apply_event(event)

    def apply_event(self, event: Any) -> None:
        payload = parse_inner_dialogue_payload(event)
        if payload is None:
            return
        kind = str(payload.get("kind") or "").strip()
        if kind == INNER_DIALOGUE_KIND:
            self._apply_sink(event, payload)
        elif kind == INNER_DIALOGUE_RETURN_KIND:
            self._apply_return(event, payload)
        elif kind == INNER_DIALOGUE_RETURN_DELIVERY_KIND:
            self._apply_delivery(event, payload)

    def _apply_sink(self, event: Any, payload: Mapping[str, Any]) -> None:
        receipt_id = str(payload.get("receipt_id") or "").strip()
        thought = str(payload.get("thought") or "")
        expect_surface = bool(payload.get("expect_surface"))
        existing = self.records.get(receipt_id)
        existing_event_id = str(payload.get("event_id") or getattr(event, "event_id", ""))
        if (
            existing is not None
            and existing.status != "internal"
            and existing.sink_event_id
            and existing.sink_event_id == existing_event_id
        ):
            return
        record = InnerDialogueRecord(
            receipt_id=receipt_id,
            sink_event_id=str(payload.get("event_id") or getattr(event, "event_id", "") or ""),
            sequence=int(payload.get("sequence") or getattr(event, "sequence", 0) or 0),
            sunk_at=str(payload.get("timestamp") or getattr(event, "timestamp", "") or ""),
            thought=thought,
            thought_sha256=str(payload.get("thought_sha256") or _sha256_text(thought)),
            thought_bytes=int(payload.get("thought_bytes") or _utf8_bytes(thought)),
            mode=str(payload.get("mode") or "reflect") or "reflect",
            expect_surface=expect_surface,
            stream_id=str(payload.get("stream_id") or "").strip(),
            source_instance_id=str(payload.get("source_instance_id") or "").strip(),
            platform=str(getattr(event, "source", "") or ""),
            chat_type=str(getattr(event, "chat_type", "") or ""),
            status="open" if expect_surface else "internal",
        )
        if existing is not None and existing.status == "returned":
            record = replace(
                record,
                status=existing.status,
                return_occurrence_id=existing.return_occurrence_id,
                return_event_id=existing.return_event_id,
                return_statement=existing.return_statement,
                return_statement_sha256=existing.return_statement_sha256,
                return_actor=existing.return_actor,
                wake_pending=existing.wake_pending,
                wake_delivered=existing.wake_delivered,
                delivery_event_id=existing.delivery_event_id,
                trigger_message_id=existing.trigger_message_id,
            )
        self.records[receipt_id] = record

    def _apply_return(self, event: Any, payload: Mapping[str, Any]) -> None:
        receipt_id = str(payload.get("receipt_id") or "").strip()
        statement = str(payload.get("statement") or "")
        occurrence = str(
            payload.get("return_occurrence_id") or payload.get("occurrence_id") or ""
        ).strip()
        statement_sha = str(
            payload.get("statement_sha256") or _sha256_text(statement)
        )
        existing = self.records.get(receipt_id)
        if existing is None:
            existing = InnerDialogueRecord(receipt_id=receipt_id, expect_surface=True)
        if (
            existing.return_occurrence_id
            and occurrence
            and existing.return_occurrence_id == occurrence
            and existing.return_statement_sha256
            and existing.return_statement_sha256 != statement_sha
        ):
            raise InnerDialogueConflict(
                receipt_id=receipt_id,
                occurrence_id=occurrence,
            )
        if (
            existing.return_occurrence_id
            and occurrence
            and existing.return_occurrence_id == occurrence
            and existing.return_statement_sha256 == statement_sha
        ):
            return
        self.records[receipt_id] = replace(
            existing,
            status="returned",
            return_occurrence_id=occurrence,
            return_event_id=str(
                payload.get("event_id") or getattr(event, "event_id", "") or ""
            ),
            return_statement=statement,
            return_statement_sha256=statement_sha,
            return_actor=str(
                payload.get("actor_consciousness_instance_id")
                or getattr(event, "source_instance_id", "")
                or ""
            ),
            stream_id=str(payload.get("stream_id") or existing.stream_id).strip(),
            wake_pending=not existing.wake_delivered,
        )

    def _apply_delivery(self, event: Any, payload: Mapping[str, Any]) -> None:
        receipt_id = str(payload.get("receipt_id") or "").strip()
        existing = self.records.get(receipt_id)
        if existing is None:
            return
        self.records[receipt_id] = replace(
            existing,
            wake_pending=False,
            wake_delivered=True,
            delivery_event_id=str(
                payload.get("event_id") or getattr(event, "event_id", "") or ""
            ),
            trigger_message_id=str(payload.get("trigger_message_id") or existing.trigger_message_id),
        )

    def mark_wake_enqueued(self, receipt_id: str, *, trigger_message_id: str) -> None:
        existing = self.get(receipt_id)
        if existing is None:
            return
        self.records[receipt_id] = replace(
            existing,
            trigger_message_id=str(trigger_message_id or existing.trigger_message_id),
        )
