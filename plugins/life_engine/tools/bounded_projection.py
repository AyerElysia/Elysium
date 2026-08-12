"""Task-aware, content-neutral bounding for model-visible tool results."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any

BOUNDED_TOOL_PROJECTION_SCHEMA = "life_engine.bounded_tool_result"
BOUNDED_TOOL_PROJECTION_VERSION = "bounded-tool-result-v1"
BOUNDED_TOOL_CURSOR_VERSION = "brc1"
CORE_TOOL_RESULT_MAX_BYTES = 8 * 1024
CHAT_TOOL_RESULT_MAX_BYTES = 16 * 1024
MIN_TOOL_RESULT_MAX_BYTES = 1024


class BoundedContinuationError(ValueError):
    """A malformed, stale, or query-mismatched model-visible continuation."""


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize identities deterministically without becoming the wire format."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def utf8_prefix(value: str, max_bytes: int) -> str:
    encoded = str(value or "").encode("utf-8")
    if len(encoded) <= max(0, int(max_bytes)):
        return str(value or "")
    return encoded[: max(0, int(max_bytes))].decode("utf-8", errors="ignore")


def serialized_utf8_bytes(value: Any) -> int:
    """Measure the exact representation returned through the tool wrapper."""

    return len(str(value).encode("utf-8"))


def resolve_tool_result_budget(
    task_name: str,
    requested_max_bytes: int | None,
) -> tuple[str, int]:
    """Resolve a task cap; unknown tasks intentionally fail safe to ``core``."""

    normalized = str(task_name or "").strip().lower()
    task_bucket = (
        normalized
        if normalized in {"expression", "life_chatter"}
        else "core"
    )
    cap = (
        CHAT_TOOL_RESULT_MAX_BYTES
        if task_bucket in {"expression", "life_chatter"}
        else CORE_TOOL_RESULT_MAX_BYTES
    )
    if requested_max_bytes is None:
        return task_bucket, cap
    requested = int(requested_max_bytes)
    if requested < MIN_TOOL_RESULT_MAX_BYTES:
        raise ValueError(
            f"max_bytes must be at least {MIN_TOOL_RESULT_MAX_BYTES}"
        )
    return task_bucket, min(requested, cap)


def _encode_legacy_cursor(state: Mapping[str, Any]) -> str:
    raw = canonical_json_bytes(dict(state))
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    checksum = hashlib.sha256(raw).hexdigest()[:16]
    return f"{body}.{checksum}"


def _decode_legacy_cursor(token: str) -> dict[str, Any]:
    try:
        body, checksum = str(token or "").split(".", 1)
        raw = base64.urlsafe_b64decode(
            (body + "=" * (-len(body) % 4)).encode("ascii")
        )
        if hashlib.sha256(raw).hexdigest()[:16] != checksum:
            raise ValueError("checksum mismatch")
        loaded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise BoundedContinuationError(
            "invalid bounded-result continuation"
        ) from exc
    if not isinstance(loaded, dict):
        raise BoundedContinuationError(
            "invalid bounded-result continuation payload"
        )
    return loaded


def _cursor_checksum(
    identity: Mapping[str, Any],
    *,
    offset_field: str,
    offset: int,
    strict: bool = False,
) -> str:
    """Checksum the cursor identity; ``strict`` excludes the frontier field.

    The frontier is a freshness signal: event-stream sources advance it
    between pages on a multi-writer deployment, so it must not participate in
    the binding checksum. It is still carried in the cursor text so strict
    (file/content) projections can compare it.
    """

    checksum_identity = (
        {
            key: value
            for key, value in identity.items()
            if key != "frontier_sha256"
        }
        if strict
        else dict(identity)
    )
    return sha256_json(
        {
            "cursor_version": BOUNDED_TOOL_CURSOR_VERSION,
            "identity": checksum_identity,
            "offset_field": offset_field,
            "offset": int(offset),
        }
    )[:16]


def _encode_cursor(
    identity: Mapping[str, Any],
    *,
    offset_field: str,
    offset: int,
) -> str:
    """Encode a short, model-copyable cursor bound to the full source identity.

    Format: ``brc1.<field>.<offset>.<frontier8|->.<checksum16>``. The frontier
    prefix is informational (compared in strict mode, tolerated in relaxed
    mode); the checksum binds only the query-semantics identity.
    """

    field_code = {"offset": "i", "byte_offset": "b"}.get(offset_field)
    if field_code is None:
        raise ValueError("unsupported bounded-result cursor offset field")
    checksum = _cursor_checksum(
        identity,
        offset_field=offset_field,
        offset=offset,
        strict=True,
    )
    frontier = identity.get("frontier_sha256")
    frontier_prefix = str(frontier)[:8] if frontier is not None else "-"
    return (
        f"{BOUNDED_TOOL_CURSOR_VERSION}.{field_code}.{int(offset)}."
        f"{frontier_prefix}.{checksum}"
    )


def _decode_cursor(
    token: str,
    *,
    identity: Mapping[str, Any],
    offset_field: str,
    tolerate_frontier_change: bool = False,
) -> tuple[int, bool]:
    """Decode compact cursors while accepting already-issued legacy cursors.

    Returns ``(offset, source_changed)``. Binding (query-semantics) mismatches
    always raise; a changed frontier raises only in strict mode.
    """

    normalized = str(token or "")
    if normalized.startswith(f"{BOUNDED_TOOL_CURSOR_VERSION}."):
        try:
            parts = normalized.split(".")
            if len(parts) == 5:
                version, field_code, raw_offset, frontier_prefix, checksum = parts
                strict = True
            elif len(parts) == 4:
                version, field_code, raw_offset, checksum = parts
                frontier_prefix = None
                strict = False
            else:
                raise ValueError("cursor format mismatch")
            expected_field_code = {"offset": "i", "byte_offset": "b"}[
                offset_field
            ]
            if version != BOUNDED_TOOL_CURSOR_VERSION:
                raise ValueError("cursor version mismatch")
            if field_code != expected_field_code:
                raise ValueError("cursor offset field mismatch")
            offset = int(raw_offset)
        except (KeyError, TypeError, ValueError) as exc:
            raise BoundedContinuationError(
                "invalid bounded-result continuation"
            ) from exc
        expected_checksum = _cursor_checksum(
            identity,
            offset_field=offset_field,
            offset=offset,
            strict=strict,
        )
        if not hmac.compare_digest(checksum, expected_checksum):
            raise BoundedContinuationError(
                "bounded-result continuation 与本次查询参数不一致："
                "续读必须携带与上一页完全相同的参数"
                "（query/order/limit/stream_ids 等），或放弃上一页重新查询"
            )
        source_changed = False
        current_frontier = identity.get("frontier_sha256")
        if (
            strict
            and current_frontier is not None
            and frontier_prefix not in {None, "-"}
            and frontier_prefix != str(current_frontier)[:8]
        ):
            if not tolerate_frontier_change:
                raise BoundedContinuationError(
                    "bounded-result continuation 与本次查询参数不一致："
                    "续读必须携带与上一页完全相同的参数"
                    "（query/order/limit/stream_ids 等），或放弃上一页重新查询"
                )
            source_changed = True
        return offset, source_changed

    state = _decode_legacy_cursor(normalized)
    for field, expected in identity.items():
        if state.get(field) != expected:
            raise BoundedContinuationError(
                "bounded-result continuation 与本次查询参数不一致："
                "续读必须携带与上一页完全相同的参数"
                "（query/order/limit/stream_ids 等），或放弃上一页重新查询"
            )
    try:
        return int(state.get(offset_field)), False
    except (TypeError, ValueError) as exc:
        raise BoundedContinuationError(
            "bounded-result continuation offset is invalid"
        ) from exc


def _finalize_delivered_bytes(payload: dict[str, Any]) -> int:
    for _ in range(12):
        actual = serialized_utf8_bytes(payload)
        if payload.get("delivered_bytes") == actual:
            return actual
        payload["delivered_bytes"] = actual
    actual = serialized_utf8_bytes(payload)
    payload["delivered_bytes"] = actual
    if serialized_utf8_bytes(payload) != actual:
        raise ValueError("bounded-result byte accounting did not converge")
    return actual


def project_bounded_items(
    *,
    projection_name: str,
    task_name: str,
    requested_max_bytes: int | None,
    binding: Mapping[str, Any],
    frontier: Mapping[str, Any] | str,
    base_payload: Mapping[str, Any],
    items_key: str,
    items: Sequence[Mapping[str, Any]],
    item_refs: Sequence[str],
    continuation: str = "",
    tolerate_frontier_change: bool = False,
) -> dict[str, Any]:
    """Project stable source items into one exact UTF-8-bounded model page.

    ``tolerate_frontier_change`` is for event-stream projections whose source is
    appended by multiple writers: the frontier advances between pages, which is
    freshness, not tampering. File/content snapshots must keep strict frontier
    binding (the source changed means the page is no longer meaningful).
    """

    name = str(projection_name or "").strip()
    key = str(items_key or "").strip()
    if not name or not key:
        raise ValueError("projection_name and items_key are required")
    if len(items) != len(item_refs):
        raise ValueError("bounded-result item refs do not match source items")

    resolved_task, budget = resolve_tool_result_budget(
        task_name,
        requested_max_bytes,
    )
    source_items = [dict(item) for item in items]
    refs = [str(ref or "").strip() for ref in item_refs]
    if any(not ref for ref in refs):
        raise ValueError("bounded-result item refs must not be empty")
    if any("_projection" in item for item in source_items):
        raise ValueError("source item uses reserved _projection field")

    frontier_sha256 = sha256_json(frontier)
    binding_sha256 = sha256_json(
        {
            "projection": name,
            "binding": dict(binding),
            "task": resolved_task,
            "budget_bytes": budget,
        }
    )
    cursor_identity = {
        "version": BOUNDED_TOOL_PROJECTION_VERSION,
        "projection": name,
        "task": resolved_task,
        "budget_bytes": budget,
        "binding_sha256": binding_sha256,
        "frontier_sha256": frontier_sha256,
    }

    start = 0
    source_changed = False
    if continuation:
        start, source_changed = _decode_cursor(
            continuation,
            identity=cursor_identity,
            offset_field="offset",
            tolerate_frontier_change=tolerate_frontier_change,
        )
        if start < 0 or start > len(source_items):
            raise BoundedContinuationError(
                "bounded-result continuation offset is invalid"
            )

    source_sizes = [serialized_utf8_bytes(item) for item in source_items]
    source_hashes = [sha256_json(item) for item in source_items]
    original_bytes = sum(source_sizes)
    original_items = len(source_items)

    def cursor_for(offset: int) -> str:
        if offset >= original_items:
            return ""
        return _encode_cursor(
            cursor_identity,
            offset_field="offset",
            offset=offset,
        )

    def build_payload(
        delivered: list[dict[str, Any]],
        *,
        next_offset: int,
        covered_source_bytes: int,
        excerpted: bool,
    ) -> dict[str, Any]:
        payload = dict(base_payload)
        payload.update(
            {
                items_key: delivered,
                "projection_schema": BOUNDED_TOOL_PROJECTION_SCHEMA,
                "projection_version": BOUNDED_TOOL_PROJECTION_VERSION,
                "projection_name": name,
                "task_name": resolved_task,
                "budget_bytes": budget,
                "source_frontier_sha256": frontier_sha256,
                "query_binding_sha256": binding_sha256,
                "page_offset": start,
                "original_bytes": original_bytes,
                "original_items": original_items,
                "delivered_bytes": 0,
                "delivered_items": len(delivered),
                "omitted_bytes": max(0, original_bytes - covered_source_bytes),
                "omitted_items": max(0, original_items - len(delivered)),
                "truncated": bool(
                    excerpted or len(delivered) != original_items
                ),
                "continuation": cursor_for(next_offset),
                **(
                    {"source_changed": True}
                    if source_changed
                    else {}
                ),
            }
        )
        _finalize_delivered_bytes(payload)
        return payload

    delivered: list[dict[str, Any]] = []
    covered_source_bytes = 0
    excerpted = False
    offset = start

    while offset < original_items:
        full_item = dict(source_items[offset])
        full_item["_projection"] = {
            "delivery": "full",
            "ref": refs[offset],
            "sha256": source_hashes[offset],
            "original_bytes": source_sizes[offset],
        }
        candidate = build_payload(
            [*delivered, full_item],
            next_offset=offset + 1,
            covered_source_bytes=covered_source_bytes + source_sizes[offset],
            excerpted=excerpted,
        )
        if serialized_utf8_bytes(candidate) <= budget:
            delivered.append(full_item)
            covered_source_bytes += source_sizes[offset]
            offset += 1
            continue
        if delivered:
            break

        source_repr = str(source_items[offset])
        excerpt_offset = offset

        def excerpt_payload(
            excerpt_bytes: int,
            *,
            source_value: str = source_repr,
            item_offset: int = excerpt_offset,
        ) -> dict[str, Any]:
            excerpt = utf8_prefix(source_value, excerpt_bytes)
            entry = {
                "excerpt": excerpt,
                "_projection": {
                    "delivery": "excerpt",
                    "ref": refs[item_offset],
                    "sha256": source_hashes[item_offset],
                    "original_bytes": source_sizes[item_offset],
                    "excerpt_bytes": len(excerpt.encode("utf-8")),
                },
            }
            return build_payload(
                [entry],
                next_offset=item_offset + 1,
                covered_source_bytes=len(excerpt.encode("utf-8")),
                excerpted=True,
            )

        low = 0
        high = len(source_repr.encode("utf-8"))
        best: dict[str, Any] | None = None
        while low <= high:
            middle = (low + high) // 2
            candidate = excerpt_payload(middle)
            if serialized_utf8_bytes(candidate) <= budget:
                best = candidate
                low = middle + 1
            else:
                high = middle - 1
        if best is None:
            raise ValueError(
                "max_bytes is too small for bounded-result projection metadata"
            )
        delivered = list(best[key])
        covered_source_bytes = int(
            delivered[0]["_projection"]["excerpt_bytes"]
        )
        excerpted = True
        offset += 1
        break

    payload = build_payload(
        delivered,
        next_offset=offset,
        covered_source_bytes=covered_source_bytes,
        excerpted=excerpted,
    )
    actual = serialized_utf8_bytes(payload)
    if actual != payload["delivered_bytes"] or actual > budget:
        raise ValueError(
            f"bounded-result projection exceeded budget: {actual} > {budget}"
        )
    return payload


def project_bounded_text(
    *,
    projection_name: str,
    task_name: str,
    requested_max_bytes: int | None,
    binding: Mapping[str, Any],
    frontier: Mapping[str, Any] | str,
    base_payload: Mapping[str, Any],
    content: str,
    content_ref: str,
    continuation: str = "",
) -> dict[str, Any]:
    """Project one immutable text snapshot as stable UTF-8 byte chunks."""

    name = str(projection_name or "").strip()
    ref = str(content_ref or "").strip()
    if not name or not ref:
        raise ValueError("projection_name and content_ref are required")
    resolved_task, budget = resolve_tool_result_budget(
        task_name,
        requested_max_bytes,
    )
    source_text = str(content or "")
    source_bytes = source_text.encode("utf-8")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    frontier_sha256 = sha256_json(frontier)
    binding_sha256 = sha256_json(
        {
            "projection": name,
            "binding": dict(binding),
            "task": resolved_task,
            "budget_bytes": budget,
        }
    )
    cursor_identity = {
        "version": BOUNDED_TOOL_PROJECTION_VERSION,
        "projection": name,
        "task": resolved_task,
        "budget_bytes": budget,
        "binding_sha256": binding_sha256,
        "frontier_sha256": frontier_sha256,
        "content_sha256": source_sha256,
    }
    start = 0
    if continuation:
        start, _ = _decode_cursor(
            continuation,
            identity=cursor_identity,
            offset_field="byte_offset",
        )
        if start < 0 or start > len(source_bytes):
            raise BoundedContinuationError(
                "bounded-text continuation offset is invalid"
            )
        try:
            source_bytes[:start].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BoundedContinuationError(
                "bounded-text continuation is not on a UTF-8 boundary"
            ) from exc

    original_items = 0 if not source_text else source_text.count("\n") + 1

    def cursor_for(end: int) -> str:
        if end >= len(source_bytes):
            return ""
        return _encode_cursor(
            cursor_identity,
            offset_field="byte_offset",
            offset=end,
        )

    def build_payload(chunk: str, end: int) -> dict[str, Any]:
        chunk_bytes = len(chunk.encode("utf-8"))
        payload = dict(base_payload)
        payload.update(
            {
                "content": chunk,
                "content_ref": ref,
                "content_sha256": source_sha256,
                "projection_schema": BOUNDED_TOOL_PROJECTION_SCHEMA,
                "projection_version": BOUNDED_TOOL_PROJECTION_VERSION,
                "projection_name": name,
                "task_name": resolved_task,
                "budget_bytes": budget,
                "source_frontier_sha256": frontier_sha256,
                "query_binding_sha256": binding_sha256,
                "page_start_byte": start,
                "page_end_byte": end,
                "original_bytes": len(source_bytes),
                "original_items": original_items,
                "delivered_bytes": 0,
                "delivered_content_bytes": chunk_bytes,
                "delivered_items": (
                    0 if not chunk else chunk.count("\n") + 1
                ),
                "omitted_bytes": max(0, len(source_bytes) - chunk_bytes),
                "omitted_items": max(
                    0,
                    original_items - (0 if not chunk else chunk.count("\n") + 1),
                ),
                "truncated": bool(start > 0 or end < len(source_bytes)),
                "continuation": cursor_for(end),
            }
        )
        _finalize_delivered_bytes(payload)
        return payload

    if start == len(source_bytes):
        payload = build_payload("", start)
        if serialized_utf8_bytes(payload) > budget:
            raise ValueError(
                "max_bytes is too small for bounded-text projection metadata"
            )
        return payload

    remaining = source_bytes[start:]
    low = 0
    high = len(remaining)
    best: dict[str, Any] | None = None
    best_chunk_bytes = 0
    while low <= high:
        middle = (low + high) // 2
        chunk = remaining[:middle].decode("utf-8", errors="ignore")
        chunk_bytes = len(chunk.encode("utf-8"))
        candidate = build_payload(chunk, start + chunk_bytes)
        if serialized_utf8_bytes(candidate) <= budget:
            best = candidate
            best_chunk_bytes = chunk_bytes
            low = middle + 1
        else:
            high = middle - 1
    if best is None or best_chunk_bytes <= 0:
        raise ValueError(
            "max_bytes is too small for bounded-text projection metadata"
        )
    actual = serialized_utf8_bytes(best)
    if actual != best["delivered_bytes"] or actual > budget:
        raise ValueError(
            f"bounded-text projection exceeded budget: {actual} > {budget}"
        )
    return best


__all__ = [
    "BOUNDED_TOOL_PROJECTION_SCHEMA",
    "BOUNDED_TOOL_PROJECTION_VERSION",
    "BOUNDED_TOOL_CURSOR_VERSION",
    "BoundedContinuationError",
    "CHAT_TOOL_RESULT_MAX_BYTES",
    "CORE_TOOL_RESULT_MAX_BYTES",
    "MIN_TOOL_RESULT_MAX_BYTES",
    "canonical_json_bytes",
    "project_bounded_items",
    "project_bounded_text",
    "resolve_tool_result_budget",
    "serialized_utf8_bytes",
    "sha256_json",
    "utf8_prefix",
]
