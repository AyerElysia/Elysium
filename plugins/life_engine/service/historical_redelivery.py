"""Bounded, continuable views of explicitly redelivered historical events.

Only the transport projection is shortened. Source identity, source bytes and
runtime provenance remain unchanged, and ordinary events retain their renderer.
"""

from __future__ import annotations

import hashlib
import json

from .event_builder import LifeEngineEvent

HISTORICAL_REDELIVERY_MAX_UTF8_BYTES = 512
_LINE_SEPARATOR_ESCAPES = str.maketrans(
    {"\u0085": r"\u0085", "\u2028": r"\u2028", "\u2029": r"\u2029"}
)


def format_historical_redelivery(event: LifeEngineEvent, body: str) -> str:
    """Wrap an already-rendered body without revealing any redacted payload.

    The SHA-256 and byte count describe the original raw body, not the excerpt or
    the entire Life Event envelope. ``occurrence_id`` is the exact argument accepted
    by ``nucleus_read_event``; long metadata fails closed instead of losing that
    continuation path. Empty renderer output cannot become deliverable via a label.
    The complete wrapper is one physical line so later whole-line budgets cannot
    separate its historical label from the continuation metadata.
    """

    if event.redelivery_operation_id is None or not body.strip():
        return body
    occurrence = event.occurrence_id
    if (
        not isinstance(occurrence, str)
        or not occurrence
        or occurrence != occurrence.strip()
    ):
        raise ValueError("HistoricalRedeliveryOccurrenceRequired")
    raw_body = event.raw_content if event.raw_content is not None else event.content
    if not isinstance(raw_body, str):
        raise TypeError("HistoricalRedeliveryRawBodyMustBeText")
    original = raw_body.encode("utf-8")
    timestamp = json.dumps(str(event.timestamp), ensure_ascii=False)[1:-1]
    header = (
        "[历史活动重投，非新发生、非工具执行请求；"
        f"原始账本位置={event.redelivery_source_position}；"
        f"原始时间={timestamp}] "
    )
    metadata = {
        "delivery": "visible_projection",
        "occurrence_id": f"life-event-occurrence:{occurrence}",
        "raw_sha256": hashlib.sha256(original).hexdigest(),
        "raw_bytes": len(original),
        "read_with": "nucleus_read_event",
        "excerpt": body,
    }

    def render() -> str:
        # JSON escapes C0 line controls; splitlines() also recognizes these
        # three Unicode separators, which ensure_ascii=False otherwise retains.
        return (
            header + json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
        ).translate(_LINE_SEPARATOR_ESCAPES)

    complete = render()
    if len(complete.encode("utf-8")) <= HISTORICAL_REDELIVERY_MAX_UTF8_BYTES:
        return complete
    metadata["delivery"] = "excerpt_ref"
    metadata["excerpt"] = ""
    if len(render().encode("utf-8")) > HISTORICAL_REDELIVERY_MAX_UTF8_BYTES:
        raise ValueError("HistoricalRedeliveryMetadataExceedsBudget")

    # Python prefixes preserve Unicode scalar boundaries; measure the actual
    # serialized UTF-8 bytes so JSON escaping also counts toward the hard cap.
    low, high = 0, len(body)
    while low < high:
        middle = (low + high + 1) // 2
        metadata["excerpt"] = body[:middle]
        if len(render().encode("utf-8")) <= HISTORICAL_REDELIVERY_MAX_UTF8_BYTES:
            low = middle
        else:
            high = middle - 1
    metadata["excerpt"] = body[:low]
    return render()
