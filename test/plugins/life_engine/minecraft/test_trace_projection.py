"""Tests for bounded Minecraft trace receipts and prompt-only perception."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from plugins.life_engine.minecraft.embodiment_contracts import (
    EmbodiedIntent,
    PerceptionReference,
)
from plugins.life_engine.minecraft.embodiment_trace import EmbodimentTrace
from plugins.life_engine.minecraft.trace_projection import (
    WORLD_TRACE_RECEIPT_MAX_BYTES,
    TraceProjectionError,
    build_world_trace_receipt,
    world_trace_receipt_size,
)


def _prepared(content: str) -> SimpleNamespace:
    """Create one complete transient delivery with stable provenance."""

    encoded = content.encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return SimpleNamespace(
        instance_id="minecraft-instance",
        projection_kind="world_perception",
        from_position=7,
        through_position=11,
        source_frontier=13,
        cursor_revision=3,
        content=content,
        assertion_ids=("assertion-7", "assertion-9"),
        change_positions=(8, 11),
        delivery_id=f"delivery-{digest[:24]}",
        projection_sha256=digest,
        algorithm_version="world-perception-page-v2",
        delivered_bytes=len(encoded),
    )


async def test_intent_trace_references_large_prompt_without_persisting_it(
    tmp_path: Path,
) -> None:
    """A 1.5 MB Perception body reaches the planner but never its trace receipt."""

    marker = "transient_world_perception"
    prompt_content = marker + ("爱莉看到的世界" * 125_000)
    reference = PerceptionReference.from_prepared(_prepared(prompt_content))
    intent = EmbodiedIntent(
        text=f"The user's legitimate text contains {marker}",
        body_name="agent",
        durable_context={"session_id": "session-a"},
        transient_prompt_context={"world_perception": prompt_content},
        perception_reference=reference,
    )

    prompt = intent.to_prompt()
    wire = intent.to_wire()
    assert prompt["transient_prompt_context"]["world_perception"] == prompt_content
    assert "transient_prompt_context" not in wire
    assert prompt_content not in json.dumps(wire, ensure_ascii=False)
    assert wire["perception_reference"]["bytes"] == len(
        prompt_content.encode("utf-8")
    )

    trace = EmbodimentTrace(tmp_path / "trace.jsonl")
    await trace.open()
    record = await trace.append("intent.issued", wire)
    receipt = build_world_trace_receipt(
        record,
        session_id="session-a",
        stream_id="game.minecraft.session-a",
        body_name="agent",
    )

    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True)
    assert prompt_content not in encoded
    assert marker not in encoded
    assert world_trace_receipt_size(receipt) <= WORLD_TRACE_RECEIPT_MAX_BYTES
    assert (await trace.verify())[-1] == record


def test_perception_reference_consumes_verified_v2_metadata() -> None:
    """PreparedPerception v2 identities pass through only when content matches."""

    content = "bounded delivered world"
    prepared = _prepared(content)
    prepared.projection_kind = "prompt_projection"
    prepared.source_frontier = 19
    prepared.delivery_id = "perception-delivery-a"
    prepared.projection_sha256 = hashlib.sha256(content.encode()).hexdigest()
    prepared.algorithm_version = "world-perception-page-v2"
    prepared.delivered_bytes = len(content.encode())

    reference = PerceptionReference.from_prepared(prepared)

    assert reference.delivery_id == prepared.delivery_id
    assert reference.content_sha256 == prepared.projection_sha256
    assert reference.version == prepared.algorithm_version
    assert reference.frontier == prepared.source_frontier
    assert reference.projection_kind == "prompt_projection"

    prepared.projection_sha256 = "0" * 64
    with pytest.raises(ValueError, match="hash does not match"):
        PerceptionReference.from_prepared(prepared)


def test_perception_reference_rejects_unversioned_preparation() -> None:
    """An old producer cannot invent a delivery identity inside Minecraft."""

    prepared = _prepared("bounded delivered world")
    del prepared.algorithm_version

    with pytest.raises(ValueError, match="missing reference fields"):
        PerceptionReference.from_prepared(prepared)


async def test_trace_projection_identity_is_stable(tmp_path: Path) -> None:
    """The same durable record always derives the same World identity."""

    trace = EmbodimentTrace(tmp_path / "trace.jsonl")
    await trace.open()
    record = await trace.append("body.selected", {"body_name": "agent"})

    first = build_world_trace_receipt(
        record,
        session_id="session-a",
        stream_id="game.minecraft.session-a",
        body_name="agent",
    )
    second = build_world_trace_receipt(
        record,
        session_id="session-a",
        stream_id="game.minecraft.session-a",
        body_name="agent",
    )

    assert first == second
    assert first["projection_id"] == second["projection_id"]


async def test_trace_projection_fails_closed_for_unknown_or_oversized_data(
    tmp_path: Path,
) -> None:
    """Unknown kinds and receipts over 8 KiB cannot enter World projection."""

    trace = EmbodimentTrace(tmp_path / "trace.jsonl")
    await trace.open()
    unknown = await trace.append("future.unknown", {"value": "opaque"})
    with pytest.raises(TraceProjectionError, match="unsupported"):
        build_world_trace_receipt(
            unknown,
            session_id="session-a",
            stream_id="game.minecraft.session-a",
            body_name="agent",
        )

    oversized = await trace.append(
        "command.issued",
        {
            "command_id": "command-a",
            "intent_id": "intent-a",
            "intent_revision": 1,
            "issued_at": "2026-08-05T00:00:00+00:00",
            "operation": "x" * 9_000,
            "parameters": {},
            "based_on_observation": None,
        },
    )
    with pytest.raises(TraceProjectionError, match="exceeds 8192"):
        build_world_trace_receipt(
            oversized,
            session_id="session-a",
            stream_id="game.minecraft.session-a",
            body_name="agent",
        )
