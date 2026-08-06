from __future__ import annotations

import hashlib
import json

from src.kernel.llm import ROLE, LLMPayload, ToolResult
from src.kernel.llm.request import _tool_result_references


def test_tool_result_reference_is_content_free_and_keeps_delivery_identity() -> None:
    value = {
        "schema": "elysium.conversation_evidence.v1",
        "delivery_id": "conversation:abc",
        "projection_sha256": "f" * 64,
        "delivered_bytes": 321,
        "items": [{"text": "private evidence"}],
    }
    rendered = json.dumps(value, ensure_ascii=False)
    refs = _tool_result_references(
        [
            LLMPayload(
                ROLE.TOOL_RESULT,
                ToolResult(value=value, call_id="call-1", name="conversation_evidence"),
            )
        ]
    )

    assert refs == [
        {
            "call_id": "call-1",
            "name": "conversation_evidence",
            "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            "utf8_bytes": len(rendered.encode("utf-8")),
            "schema": "elysium.conversation_evidence.v1",
            "delivery_id": "conversation:abc",
            "projection_sha256": "f" * 64,
            "delivered_bytes": 321,
        }
    ]
    assert "private evidence" not in json.dumps(refs)
