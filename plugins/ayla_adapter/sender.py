"""Content-preserving virtual acknowledgement for Ayla outbound messages."""

from __future__ import annotations

import hashlib
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.core.transport.wire import MessageEnvelope

logger = get_logger("ayla_adapter")


class AylaSender:
    """Acknowledge the common outbound path without delivering a second copy.

    Ayla's backend consumes the durable ``chat.message`` delivered event over
    SSE.  Posting from this adapter as well would duplicate the same reply.
    """

    async def send(self, envelope: MessageEnvelope) -> None:
        segments = envelope.get("message_segment", [])
        segment_count = len(segments) if isinstance(segments, list) else 0
        preview = self._extract_text_preview(envelope)
        text_sha256 = hashlib.sha256(preview.encode("utf-8")).hexdigest()
        logger.debug(
            "Ayla 出站已虚拟确认，由 SSE 交付投影负责实际投递: "
            f"segment_count={segment_count}, "
            f"preview_chars={len(preview)}, preview_sha256={text_sha256}"
        )

    @staticmethod
    def _extract_text_preview(envelope: MessageEnvelope | dict[str, Any]) -> str:
        """Return at most fifty content characters for bounded diagnostics."""

        parts: list[str] = []
        segments = envelope.get("message_segment", [])
        if isinstance(segments, list):
            for segment in segments:
                if not isinstance(segment, dict) or segment.get("type") != "text":
                    continue
                data = segment.get("data", "")
                if isinstance(data, dict):
                    data = data.get("text", data.get("content", ""))
                if data:
                    parts.append(str(data))
        if not parts:
            content = envelope.get("content", "")
            if content:
                parts.append(str(content))
        text = "".join(parts)
        return text if len(text) <= 50 else text[:50] + "…"
