"""Kernel-level acceptance tests for exact Memory Witness World delivery.

These tests keep the production ``MemoryWitnessCoordinator._author_witness``
path and the real LLM request/context/response stack.  Only model routing,
synthetic prompt inputs, and the network-facing model client are replaced.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.life_engine.memory.experience import (
    ExperienceOccurrenceRef,
    ExperienceRecord,
)
from plugins.life_engine.service import memory_witness as witness_module
from plugins.life_engine.service.consciousness import ConsciousnessInstance
from plugins.life_engine.service.memory_witness import (
    MEMORY_WITNESS_INSTANCE_ID,
    MemoryWitnessCoordinator,
)
from plugins.life_engine.service.perception_gateway import PreparedPerception
from src.kernel.llm import request as request_module
from src.kernel.llm.context import LLMContextManager
from src.kernel.llm.exceptions import LLMAPIError
from src.kernel.llm.model_client import ModelClientRegistry, StreamEvent
from src.kernel.llm.payload import LLMPayload, Text
from src.kernel.llm.request import LLMRequest


@dataclass(frozen=True, slots=True)
class _Attempt:
    model_name: str
    stream: bool
    text_parts: tuple[str, ...]


class _FakeNetworkClient:
    """Deterministic network boundary that never opens a real connection."""

    def __init__(
        self,
        *,
        failures: set[str] | None = None,
        request_ids: dict[str, int] | None = None,
        message: str = "synthetic witness",
    ) -> None:
        self.failures = set(failures or ())
        self.request_ids = dict(request_ids or {})
        self.message = message
        self.attempts: list[_Attempt] = []

    async def create(
        self,
        *,
        model_name: str,
        payloads: list[LLMPayload],
        tools: list[Any],
        request_name: str,
        model_set: Any,
        stream: bool,
    ) -> tuple[Any, ...]:
        del tools, request_name, model_set
        text_parts = tuple(
            part.text
            for payload in payloads
            for part in payload.content
            if isinstance(part, Text)
        )
        self.attempts.append(
            _Attempt(
                model_name=model_name,
                stream=stream,
                text_parts=text_parts,
            )
        )
        if model_name in self.failures:
            raise LLMAPIError("synthetic upstream failure", status_code=500)

        request_id = self.request_ids.get(model_name, 1)
        if not stream:
            return self.message, [], None, None, request_id

        async def _stream_events():
            yield StreamEvent(text_delta=self.message)

        return None, [], _stream_events(), None, request_id


class _NoopMetricsCollector:
    def record_request(self, _metrics: Any) -> None:
        return


class _WitnessService:
    def __init__(self, perception: PreparedPerception) -> None:
        self._perception = perception
        self._config = SimpleNamespace(
            memory_witness=SimpleNamespace(
                model_task_name="witness-kernel-test",
                timeout_seconds=5.0,
            )
        )
        source_digest = "a" * 64
        text = f"""# Subject Context Projection

- source_digest: `{source_digest}`
- projection_version: `1`

<subject-source path="SOUL.md">
synthetic SOUL projection
</subject-source>

<subject-source path="USER.md">
synthetic USER projection
</subject-source>

<subject-source path="MEMORY.md">
synthetic MEMORY projection
</subject-source>"""
        self._subject_snapshot = {
            "text": text,
            "source_digest": source_digest,
            "projection_version": 1,
            "projection_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    def _cfg(self) -> Any:
        return self._config

    async def prepare_perception(self, instance_id: str) -> PreparedPerception:
        assert instance_id == MEMORY_WITNESS_INSTANCE_ID
        return self._perception

    async def get_subject_context_projection_snapshot(
        self,
        *,
        projection_kind: str,
        max_bytes: int,
    ) -> dict[str, Any]:
        assert projection_kind == "memory_witness"
        assert max_bytes == 24 * 1024
        return dict(self._subject_snapshot)


def _model(
    name: str,
    *,
    context_tokens: int = 50_000,
    force_stream_mode: bool = False,
    route_scope: str = "default",
) -> dict[str, Any]:
    return {
        "api_provider": "openai",
        "base_url": f"https://{route_scope}.invalid/v1",
        "model_identifier": name,
        "api_key": "test-only-key",
        "client_type": "openai",
        "max_retry": 0,
        "timeout": 1.0,
        "retry_interval": 0.0,
        "price_in": 0.0,
        "price_out": 0.0,
        "temperature": 0.0,
        "max_tokens": 64,
        "max_context": 60_000,
        "context_tokens": context_tokens,
        "force_stream_mode": force_stream_mode,
        "tool_call_compat": False,
        "extra_params": {},
    }


def _perception(*, body_chars: int, identity: str) -> PreparedPerception:
    delivery_id = f"world-{identity}"
    content = f"world-perception:{delivery_id}\n" + ("w" * body_chars)
    encoded = content.encode("utf-8")
    return PreparedPerception(
        instance_id=MEMORY_WITNESS_INSTANCE_ID,
        projection_kind="memory_witness",
        from_position=2,
        through_position=5,
        source_frontier=5,
        cursor_revision=7,
        content=content,
        assertion_ids=(),
        change_positions=(),
        delivery_id=delivery_id,
        projection_sha256=hashlib.sha256(encoded).hexdigest(),
        algorithm_version="kernel-test-v1",
        delivered_bytes=len(encoded),
        source_payload_bytes=len(encoded),
        omitted_assertion_count=0,
        omitted_change_count=0,
        omitted_source_bytes=0,
        snapshot_continuation_token="",
        has_more_changes=False,
    )


def _occurrence() -> ExperienceOccurrenceRef:
    record = ExperienceRecord(
        event_id="event-kernel-delivery",
        sequence=4,
        occurred_at="2026-08-13T10:00:00+08:00",
        recorded_at="2026-08-13T10:00:01+08:00",
        source="kernel-test",
        channel="test",
        event_type="synthetic_event",
        content="synthetic immutable experience",
        source_event_id="source-event-kernel-delivery",
        stream_id="stream-kernel-delivery",
        consciousness_instance_id=MEMORY_WITNESS_INSTANCE_ID,
        actor="synthetic-actor",
    )
    digest = hashlib.sha256(record.content.encode("utf-8")).hexdigest()
    return ExperienceOccurrenceRef(
        occurrence_id="occurrence-kernel-delivery",
        source_event_id=record.source_event_id,
        ingest_position=4,
        canonical_event_id=record.event_id,
        canonical_payload_sha256=digest,
        recorded_at=record.recorded_at,
        experience=record,
    )


def _install_real_kernel_harness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    model_set: list[dict[str, Any]],
    client: _FakeNetworkClient,
) -> None:
    registry = ModelClientRegistry(openai=client, anthropic=client)
    monkeypatch.setattr(
        witness_module,
        "get_model_set_by_task",
        lambda task_name: model_set if task_name == "witness-kernel-test" else [],
    )
    monkeypatch.setattr(
        request_module,
        "get_default_model_client_registry",
        lambda: registry,
    )

    def _count_text_chars(
        payloads: list[LLMPayload],
        model_identifier: str | None = None,
    ) -> int:
        del model_identifier
        return sum(
            len(part.text)
            for payload in payloads
            for part in payload.content
            if isinstance(part, Text)
        )

    monkeypatch.setattr(request_module, "count_payload_tokens", _count_text_chars)
    monkeypatch.setattr(
        request_module,
        "classify_exception",
        lambda error, model=None: error,
    )
    monkeypatch.setattr(
        request_module,
        "get_global_collector",
        lambda: _NoopMetricsCollector(),
    )
    monkeypatch.setattr(request_module, "record_trajectory", lambda *_a, **_k: None)
    monkeypatch.setattr(
        request_module,
        "_trajectory_settings",
        lambda: (False, str(tmp_path / "trajectory"), 0.0, 1, 1, 0),
    )

    # Guard the central acceptance premise: production did not get replaced by
    # a request/response stub while installing the network fake.
    assert witness_module.LLMRequest is LLMRequest


def _coordinator(perception: PreparedPerception) -> MemoryWitnessCoordinator:
    return MemoryWitnessCoordinator(_WitnessService(perception))


def _instance() -> ConsciousnessInstance:
    return ConsciousnessInstance(
        instance_id=MEMORY_WITNESS_INSTANCE_ID,
        kind="memory_witness",
    )


def _marker_parts(
    attempt: _Attempt,
    perception: PreparedPerception,
) -> tuple[str, ...]:
    return tuple(
        text for text in attempt.text_parts if perception.delivery_marker in text
    )


@pytest.mark.asyncio
async def test_witness_accepts_untrimmed_exact_world_through_real_kernel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    perception = _perception(body_chars=256, identity="exact")
    client = _FakeNetworkClient(request_ids={"exact-model": 101})
    _install_real_kernel_harness(
        monkeypatch,
        tmp_path,
        model_set=[_model("exact-model", route_scope="exact")],
        client=client,
    )

    authored = await _coordinator(perception)._author_witness(
        _instance(),
        [_occurrence()],
    )

    assert authored.text == "synthetic witness"
    assert authored.world_payload["proof_state"] == "exact_final_attempt"
    assert authored.world_payload["receipt"]["transport_request_id"] == "101"
    assert len(client.attempts) == 1
    assert _marker_parts(client.attempts[0], perception) == (perception.content,)


@pytest.mark.asyncio
async def test_witness_fails_closed_when_real_context_manager_trims_world(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    perception = _perception(body_chars=12_000, identity="trimmed")
    client = _FakeNetworkClient(request_ids={"trim-model": 102})
    _install_real_kernel_harness(
        monkeypatch,
        tmp_path,
        model_set=[
            _model(
                "trim-model",
                context_tokens=4_096,
                route_scope="trimmed",
            )
        ],
        client=client,
    )

    with pytest.raises(
        RuntimeError,
        match="MemoryWitnessPerceptionDeliveryUnverified",
    ):
        await _coordinator(perception)._author_witness(
            _instance(),
            [_occurrence()],
        )

    assert len(client.attempts) == 1
    delivered = _marker_parts(client.attempts[0], perception)
    assert len(delivered) == 1
    assert delivered[0] != perception.content
    assert len(delivered[0]) < len(perception.content)


@pytest.mark.asyncio
@pytest.mark.parametrize("final_attempt_exact", (True, False))
async def test_witness_uses_only_final_failover_attempt_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    final_attempt_exact: bool,
) -> None:
    identity = "failover-final-exact" if final_attempt_exact else "failover-final-trim"
    perception = _perception(body_chars=12_000, identity=identity)
    client = _FakeNetworkClient(
        failures={f"{identity}-primary"},
        request_ids={f"{identity}-fallback": 203},
    )
    narrow = 4_096
    wide = 50_000
    primary_budget = narrow if final_attempt_exact else wide
    fallback_budget = wide if final_attempt_exact else narrow
    _install_real_kernel_harness(
        monkeypatch,
        tmp_path,
        model_set=[
            _model(
                f"{identity}-primary",
                context_tokens=primary_budget,
                route_scope=identity,
            ),
            _model(
                f"{identity}-fallback",
                context_tokens=fallback_budget,
                route_scope=identity,
            ),
        ],
        client=client,
    )

    if final_attempt_exact:
        authored = await _coordinator(perception)._author_witness(
            _instance(),
            [_occurrence()],
        )
        assert authored.world_payload["receipt"]["transport_request_id"] == "203"
    else:
        with pytest.raises(
            RuntimeError,
            match="MemoryWitnessPerceptionDeliveryUnverified",
        ):
            await _coordinator(perception)._author_witness(
                _instance(),
                [_occurrence()],
            )

    assert [attempt.model_name for attempt in client.attempts] == [
        f"{identity}-primary",
        f"{identity}-fallback",
    ]
    primary_parts = _marker_parts(client.attempts[0], perception)
    fallback_parts = _marker_parts(client.attempts[1], perception)
    assert (primary_parts == (perception.content,)) is not final_attempt_exact
    assert (fallback_parts == (perception.content,)) is final_attempt_exact


@pytest.mark.asyncio
@pytest.mark.parametrize("force_stream_mode", (False, True))
async def test_witness_receipt_survives_response_consumption_modes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    force_stream_mode: bool,
) -> None:
    mode = "forced-stream" if force_stream_mode else "non-stream"
    perception = _perception(body_chars=256, identity=mode)
    model_name = f"{mode}-model"
    client = _FakeNetworkClient(request_ids={model_name: 304})
    _install_real_kernel_harness(
        monkeypatch,
        tmp_path,
        model_set=[
            _model(
                model_name,
                force_stream_mode=force_stream_mode,
                route_scope=mode,
            )
        ],
        client=client,
    )

    authored = await _coordinator(perception)._author_witness(
        _instance(),
        [_occurrence()],
    )

    assert authored.text == "synthetic witness"
    assert authored.world_payload["receipt"]["exact"] is True
    assert authored.world_payload["receipt"]["transport_request_id"] == "304"
    assert [attempt.stream for attempt in client.attempts] == [force_stream_mode]
    assert _marker_parts(client.attempts[0], perception) == (perception.content,)


def test_witness_kernel_delivery_uses_real_context_manager_type() -> None:
    request = LLMRequest(
        [_model("type-proof-model", route_scope="type-proof")],
        "type-proof",
        clients=ModelClientRegistry(
            openai=_FakeNetworkClient(),
            anthropic=_FakeNetworkClient(),
        ),
        enable_metrics=False,
    )

    assert type(request) is LLMRequest
    assert type(request.context_manager) is LLMContextManager
