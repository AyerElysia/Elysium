"""Bounded factual receipts from durable Minecraft embodiment traces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from .embodiment_trace import TraceRecord

WORLD_TRACE_PROJECTION_SCHEMA = "minecraft.embodied_trace_projection.v1"
WORLD_TRACE_RECEIPT_MAX_BYTES = 8 * 1024


class TraceProjectionError(ValueError):
    """Raised when a trace cannot be projected without unsafe ambiguity."""


def _canonical_json(value: Any) -> bytes:
    """Encode one value deterministically for hashes and byte budgets."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exception:
        raise TraceProjectionError("trace projection value is not JSON-safe") from exception


def _digest(value: Any) -> str:
    """Return one canonical SHA-256 evidence digest."""

    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _required_text(payload: Mapping[str, Any], field: str) -> str:
    """Read one required non-empty string without inventing a fallback."""

    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise TraceProjectionError(f"trace payload requires text field: {field}")
    return value


def _required_int(payload: Mapping[str, Any], field: str) -> int:
    """Read one required integer while rejecting booleans and coercion."""

    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TraceProjectionError(f"trace payload requires integer field: {field}")
    return value


def _required_version(payload: Mapping[str, Any], field: str) -> int | str:
    """Read a non-empty integer or text protocol version."""

    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TraceProjectionError(f"trace payload requires version field: {field}")
    if isinstance(value, str) and not value.strip():
        raise TraceProjectionError(f"trace payload has empty version field: {field}")
    return value


def _optional_text(payload: Mapping[str, Any], field: str) -> str | None:
    """Read an optional non-empty string with strict type validation."""

    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TraceProjectionError(f"trace payload has invalid text field: {field}")
    return value


def _required_bool(payload: Mapping[str, Any], field: str) -> bool:
    """Read one required protocol boolean."""

    value = payload.get(field)
    if not isinstance(value, bool):
        raise TraceProjectionError(f"trace payload requires boolean field: {field}")
    return value


def _required_string_list(payload: Mapping[str, Any], field: str) -> list[str]:
    """Read a list of non-empty evidence identities without interpreting it."""

    value = payload.get(field)
    if not isinstance(value, list):
        raise TraceProjectionError(f"trace payload requires list field: {field}")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise TraceProjectionError(f"trace payload has invalid identities: {field}")
    return list(value)


def _perception_reference(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    """Keep only bounded delivery coordinates from a content-free reference."""

    raw = payload.get("perception_reference")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise TraceProjectionError("perception_reference must be an object")
    if raw.get("schema") != "minecraft.perception_reference.v1":
        raise TraceProjectionError("unknown Minecraft perception reference schema")
    return {
        "schema": raw["schema"],
        "delivery_id": _required_text(raw, "delivery_id"),
        "hash": _required_text(raw, "hash"),
        "version": _required_version(raw, "version"),
        "instance_id": _required_text(raw, "instance_id"),
        "projection_kind": _required_text(raw, "projection_kind"),
        "from": _required_int(raw, "from"),
        "through": _required_int(raw, "through"),
        "frontier": _required_int(raw, "frontier"),
        "cursor_revision": _required_int(raw, "cursor_revision"),
        "bytes": _required_int(raw, "bytes"),
    }


def _kind_receipt(record: TraceRecord) -> dict[str, Any]:
    """Project one known trace kind through an explicit factual allowlist."""

    payload = record.payload
    kind = record.kind
    if kind == "body.selected":
        return {"body_name": _required_text(payload, "body_name")}
    if kind == "intent.issued":
        return {
            "intent_id": _required_text(payload, "intent_id"),
            "intent_revision": _required_int(payload, "revision"),
            "issued_at": _required_text(payload, "issued_at"),
            "body_name": _required_text(payload, "body_name"),
            "intent_text_sha256": _digest(_required_text(payload, "text")),
            "durable_context_sha256": _digest(payload.get("durable_context", {})),
            "perception_reference": _perception_reference(payload),
        }
    if kind == "intent.deadline":
        timeout = payload.get("timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise TraceProjectionError("intent.deadline requires numeric timeout_seconds")
        return {
            "intent_id": _required_text(payload, "intent_id"),
            "timeout_seconds": timeout,
        }
    if kind == "intent.interrupted":
        reason = _required_text(payload, "reason")
        return {
            "intent_id": _required_text(payload, "intent_id"),
            "reason_sha256": _digest(reason),
        }
    if kind == "observation":
        return {
            "observation_id": _required_text(payload, "observation_id"),
            "game_instance_id": _required_text(payload, "instance_id"),
            "observation_sequence": _required_int(payload, "sequence"),
            "observed_at": _required_text(payload, "observed_at"),
            "received_at": _required_text(payload, "received_at"),
            "source": _required_text(payload, "source"),
            "facts_sha256": _digest(payload.get("facts", {})),
            "frame_present": payload.get("frame_path") is not None,
        }
    if kind == "command.issued":
        return {
            "command_id": _required_text(payload, "command_id"),
            "intent_id": _required_text(payload, "intent_id"),
            "intent_revision": _required_int(payload, "intent_revision"),
            "issued_at": _required_text(payload, "issued_at"),
            "operation": _required_text(payload, "operation"),
            "based_on_observation": _optional_text(
                payload, "based_on_observation"
            ),
            "parameters_sha256": _digest(payload.get("parameters", {})),
        }
    if kind == "command.receipt":
        error = _optional_text(payload, "error")
        observation_sequence = payload.get("observation_sequence")
        if observation_sequence is not None and (
            isinstance(observation_sequence, bool)
            or not isinstance(observation_sequence, int)
        ):
            raise TraceProjectionError(
                "command.receipt has invalid observation_sequence"
            )
        return {
            "receipt_id": _required_text(payload, "receipt_id"),
            "command_id": _required_text(payload, "command_id"),
            "intent_id": _required_text(payload, "intent_id"),
            "accepted": _required_bool(payload, "accepted"),
            "completed": _required_bool(payload, "completed"),
            "interrupted": _required_bool(payload, "interrupted"),
            "recorded_at": _required_text(payload, "recorded_at"),
            "observation_sequence": observation_sequence,
            "facts_sha256": _digest(payload.get("facts", {})),
            "error_present": error is not None,
            "error_sha256": _digest(error) if error is not None else None,
        }
    if kind == "intent.conclusion":
        statement = _required_text(payload, "statement")
        return {
            "intent_id": _required_text(payload, "intent_id"),
            "authored_at": _required_text(payload, "authored_at"),
            "evidence_ids": _required_string_list(payload, "evidence_ids"),
            "statement_sha256": _digest(statement),
        }
    raise TraceProjectionError(f"unsupported Minecraft trace kind: {kind}")


def build_world_trace_receipt(
    record: TraceRecord,
    *,
    session_id: str,
    stream_id: str,
    body_name: str,
) -> dict[str, Any]:
    """Build an idempotent, content-free World receipt under the hard budget."""

    if not session_id.strip() or not stream_id.strip() or not body_name.strip():
        raise TraceProjectionError("trace projection requires session, stream, and body")
    identity_material = (
        f"{session_id}\0{record.sequence}\0{record.record_hash}".encode()
    )
    projection_id = "minecraft_trace_" + hashlib.sha256(identity_material).hexdigest()
    receipt = {
        "schema": WORLD_TRACE_PROJECTION_SCHEMA,
        "projection_id": projection_id,
        "authority": "durable_trace",
        "session_id": session_id,
        "stream_id": stream_id,
        "body_name": body_name,
        "trace_kind": record.kind,
        "trace_sequence": record.sequence,
        "recorded_at": record.recorded_at,
        "record_hash": record.record_hash,
        "previous_hash": record.previous_hash,
        "payload_sha256": _digest(record.payload),
        "trace_ref": {
            "session_id": session_id,
            "sequence": record.sequence,
            "record_hash": record.record_hash,
        },
        "receipt": _kind_receipt(record),
    }
    encoded = _canonical_json(receipt)
    if len(encoded) > WORLD_TRACE_RECEIPT_MAX_BYTES:
        raise TraceProjectionError(
            "Minecraft World trace receipt exceeds 8192 UTF-8 bytes"
        )
    return receipt


def world_trace_receipt_size(receipt: Mapping[str, Any]) -> int:
    """Return the canonical UTF-8 size used by the production hard limit."""

    return len(_canonical_json(receipt))
