"""Bounded diagnostic projection for the retired ThoughtStream archive.

This module is not a model-facing tool surface. Live consciousness uses
``nucleus_proactive_query`` / ``nucleus_proactive_command``. The snapshot
file is never written here.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from pathlib import Path

from .archive import LegacyThoughtStreamArchive

THOUGHT_STREAM_LIST_DEFAULT_PAGE_SIZE = 20
THOUGHT_STREAM_LIST_MAX_PAGE_SIZE = 20
THOUGHT_STREAM_LIST_DEFAULT_MAX_BYTES = 16 * 1024
THOUGHT_STREAM_LIST_MIN_BYTES = 2 * 1024
THOUGHT_STREAM_LIST_MAX_BYTES = 16 * 1024
_LIST_CURSOR_VERSION = 1


class ThoughtStreamProjectionError(ValueError):
    """旧 ThoughtStream 有界查询无法安全继续。"""


def _encode_list_cursor(
    *,
    offset: int,
    source_revision: int,
    include_dormant: bool,
) -> str:
    payload = json.dumps(
        {
            "include_dormant": include_dormant,
            "offset": offset,
            "source_revision": source_revision,
            "version": _LIST_CURSOR_VERSION,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"{encoded}.{digest}"


def _decode_list_cursor(
    cursor: str,
    *,
    source_revision: int,
    include_dormant: bool,
) -> int:
    try:
        encoded, supplied_digest = cursor.split(".", 1)
        padding = "=" * (-len(encoded) % 4)
        payload = base64.urlsafe_b64decode(encoded + padding)
        expected_digest = hashlib.sha256(payload).hexdigest()[:16]
        if not hmac.compare_digest(supplied_digest, expected_digest):
            raise ThoughtStreamProjectionError("cursor integrity check failed")
        decoded = json.loads(payload.decode("utf-8"))
    except ThoughtStreamProjectionError:
        raise
    except (binascii.Error, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ThoughtStreamProjectionError("cursor is malformed") from exc

    if not isinstance(decoded, dict):
        raise ThoughtStreamProjectionError("cursor payload must be an object")
    if decoded.get("version") != _LIST_CURSOR_VERSION:
        raise ThoughtStreamProjectionError("cursor version is unsupported")
    if decoded.get("include_dormant") is not include_dormant:
        raise ThoughtStreamProjectionError("cursor filter does not match this query")
    if decoded.get("source_revision") != source_revision:
        raise ThoughtStreamProjectionError(
            "cursor is stale because the ThoughtStream snapshot changed"
        )
    offset = decoded.get("offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ThoughtStreamProjectionError("cursor offset is invalid")
    return offset


def _truncate_utf8(value: str, max_bytes: int) -> tuple[str, int]:
    normalized = str(value or "").replace("\r", " ").replace("\n", " ")
    raw = normalized.encode("utf-8")
    if len(raw) <= max_bytes:
        return normalized, 0

    suffix = "…"
    suffix_bytes = suffix.encode("utf-8")
    clipped = raw[: max(0, max_bytes - len(suffix_bytes))]
    text = clipped.decode("utf-8", errors="ignore")
    delivered_source_bytes = len(text.encode("utf-8"))
    return text + suffix, len(raw) - delivered_source_bytes


def _render_stream_projection(stream) -> tuple[str, int]:
    stream_id, id_omitted = _truncate_utf8(stream.id, 96)
    title, title_omitted = _truncate_utf8(stream.title, 256)
    status_tag = f"[{stream.status}]" if stream.status != "active" else ""
    lines = [
        (
            f"- {stream_id}: {title} {status_tag}"
            f" (好奇心: {stream.curiosity_score:.0%}, 推进: {stream.advance_count}次)"
        )
    ]
    thought_omitted = 0
    if stream.last_thought:
        thought, thought_omitted = _truncate_utf8(stream.last_thought, 512)
        lines.append(f"  最近想法: {thought}")
    return "\n".join(lines), id_omitted + title_omitted + thought_omitted


def _projection_meta(
    *,
    source_revision: int,
    returned: int,
    remaining: int,
    payload_bytes: int,
    omitted_field_bytes: int,
    next_cursor: str,
) -> str:
    return (
        "[projection_meta "
        f"source_revision={source_revision} "
        f"returned={returned} "
        f"remaining={remaining} "
        f"payload_bytes={payload_bytes} "
        f"omitted_field_bytes={omitted_field_bytes} "
        f"has_more={'true' if next_cursor else 'false'} "
        f"next_cursor={next_cursor or '-'}]"
    )


def _validate_list_bounds(*, page_size: int, max_bytes: int) -> None:
    if not 1 <= page_size <= THOUGHT_STREAM_LIST_MAX_PAGE_SIZE:
        raise ThoughtStreamProjectionError(
            f"page_size must be between 1 and {THOUGHT_STREAM_LIST_MAX_PAGE_SIZE}"
        )
    if not THOUGHT_STREAM_LIST_MIN_BYTES <= max_bytes <= THOUGHT_STREAM_LIST_MAX_BYTES:
        raise ThoughtStreamProjectionError(
            "max_bytes must be between "
            f"{THOUGHT_STREAM_LIST_MIN_BYTES} and {THOUGHT_STREAM_LIST_MAX_BYTES}"
        )


def _render_bounded_list(
    manager: LegacyThoughtStreamArchive,
    *,
    include_dormant: bool,
    cursor: str,
    page_size: int,
    max_bytes: int,
) -> str:
    _validate_list_bounds(page_size=page_size, max_bytes=max_bytes)

    streams = manager.list_for_projection(include_dormant=include_dormant)
    source_revision = manager.current_revision
    offset = 0
    if cursor.strip():
        offset = _decode_list_cursor(
            cursor.strip(),
            source_revision=source_revision,
            include_dormant=include_dormant,
        )
    if offset > len(streams):
        raise ThoughtStreamProjectionError("cursor offset is beyond this snapshot")

    accepted: list[str] = []
    omitted_field_bytes = 0
    candidates = streams[offset : offset + page_size]
    for stream in candidates:
        rendered, item_omitted_bytes = _render_stream_projection(stream)
        trial_items = [*accepted, rendered]
        trial_body = "\n".join(trial_items)
        trial_offset = offset + len(trial_items)
        has_more = trial_offset < len(streams)
        next_cursor = (
            _encode_list_cursor(
                offset=trial_offset,
                source_revision=source_revision,
                include_dormant=include_dormant,
            )
            if has_more
            else ""
        )
        trial_omitted = omitted_field_bytes + item_omitted_bytes
        meta = _projection_meta(
            source_revision=source_revision,
            returned=len(trial_items),
            remaining=len(streams) - trial_offset,
            payload_bytes=len(trial_body.encode("utf-8")),
            omitted_field_bytes=trial_omitted,
            next_cursor=next_cursor,
        )
        trial_result = f"{trial_body}\n\n{meta}"
        if len(trial_result.encode("utf-8")) > max_bytes:
            break
        accepted = trial_items
        omitted_field_bytes = trial_omitted

    if streams[offset:] and not accepted:
        raise ThoughtStreamProjectionError(
            "max_bytes cannot fit one complete bounded projection item"
        )

    body = "\n".join(accepted)
    final_offset = offset + len(accepted)
    has_more = final_offset < len(streams)
    next_cursor = (
        _encode_list_cursor(
            offset=final_offset,
            source_revision=source_revision,
            include_dormant=include_dormant,
        )
        if has_more
        else ""
    )
    if not body:
        body = "当前没有符合筛选条件的思考流"
    meta = _projection_meta(
        source_revision=source_revision,
        returned=len(accepted),
        remaining=len(streams) - final_offset,
        payload_bytes=len(body.encode("utf-8")),
        omitted_field_bytes=omitted_field_bytes,
        next_cursor=next_cursor,
    )
    result = f"{body}\n\n{meta}"
    if len(result.encode("utf-8")) > max_bytes:
        raise ThoughtStreamProjectionError("projection metadata exceeded max_bytes")
    return result


def read_legacy_thought_stream_page(
    workspace: str | Path,
    *,
    include_dormant: bool = False,
    cursor: str = "",
    page_size: int = THOUGHT_STREAM_LIST_DEFAULT_PAGE_SIZE,
    max_bytes: int = THOUGHT_STREAM_LIST_DEFAULT_MAX_BYTES,
) -> str:
    """Return one bounded diagnostic page of the retired ThoughtStream snapshot.

    Missing snapshots raise ``LegacySnapshotNotFoundError``. Invalid page
    bounds raise ``ThoughtStreamProjectionError``. The snapshot is not written.
    """
    archive = LegacyThoughtStreamArchive.open(
        Path(workspace).resolve() / "thoughts" / "streams.json"
    )
    return _render_bounded_list(
        archive,
        include_dormant=include_dormant,
        cursor=cursor,
        page_size=page_size,
        max_bytes=max_bytes,
    )
