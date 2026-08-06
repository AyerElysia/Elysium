from __future__ import annotations

import json

from scripts.export_training_data import (
    collapse_agent_traces,
    to_agent_trajectory,
    to_sft_chat,
)


def _record(*, attempt: int, timestamp: str, content: str) -> dict:
    return {
        "trace_id": "trace-1",
        "request_id": f"request-{attempt}",
        "attempt_id": f"attempt-{attempt}",
        "timestamp": timestamp,
        "success": True,
        "messages": [
            {"role": "user", "content": "question"},
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "conversation_evidence",
                "content": content,
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "name": "conversation_evidence",
                "content": content,
            },
        ],
        "response": {"content": "answer"},
        "tool_results": [
            {"call_id": "call-1", "name": "conversation_evidence", "content": content}
        ],
        "metadata": {"attempt_index": attempt},
    }


def test_agent_export_collapses_tool_turns_and_externalizes_large_evidence() -> None:
    old = _record(attempt=1, timestamp="2026-08-06T00:00:00Z", content="old")
    final = _record(
        attempt=2, timestamp="2026-08-06T00:00:01Z", content="证据" * 10_000
    )

    collapsed = collapse_agent_traces([old, final])
    assert len(collapsed) == 1
    sample = to_agent_trajectory(collapsed[0])
    assert sample is not None
    tool_messages = [item for item in sample["messages"] if item.get("role") == "tool"]
    assert len(tool_messages) == 1
    descriptor = json.loads(tool_messages[0]["content"])
    assert descriptor["schema"] == "elysium.training.external_evidence_ref.v1"
    assert descriptor["supervision"] == "external_evidence_not_persona_target"
    assert sample["meta"]["collapsed_record_count"] == 2
    assert len(sample["tool_results"]) == 1
    assert sample["tool_results"][0]["externalized"] is True
    assert json.loads(sample["tool_results"][0]["content"])["sha256"]
    assert (
        sample["meta"]["supervision_boundaries"]["tool_results"]
        == "external_evidence_not_persona_target"
    )


def test_conflicting_duplicate_tool_result_is_rejected() -> None:
    record = _record(attempt=1, timestamp="2026-08-06T00:00:00Z", content="one")
    record["messages"][-1]["content"] = "different"
    assert to_agent_trajectory(record) is None
    assert to_sft_chat(record) is None


def test_sft_export_keeps_evidence_as_input_not_persona_target() -> None:
    record = _record(
        attempt=1,
        timestamp="2026-08-06T00:00:00Z",
        content="external evidence" * 2_000,
    )
    sample = to_sft_chat(record)
    assert sample is not None
    tool_messages = [item for item in sample["messages"] if item["role"] == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["supervision"] == (
        "external_evidence_not_persona_target"
    )
    assert sample["meta"]["supervision_boundaries"]["assistant_messages"] == (
        "subject_output_candidate"
    )
